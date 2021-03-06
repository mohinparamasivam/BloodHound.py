####################
#
# Copyright (c) 2018 Fox-IT
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
####################
from __future__ import unicode_literals
import logging
import threading
from multiprocessing import Pool
from ldap3.utils.conv import escape_filter_chars
from impacket.uuid import string_to_bin, bin_to_string
from bloodhound.ad.utils import ADUtils
from bloodhound.lib import cstruct
from io import BytesIO
import binascii
import pprint
from future.utils import iteritems, native_str

# Extended rights and property GUID mapping, converted to binary so we don't have to do this
# for every comparison.
# Source: https://msdn.microsoft.com/en-us/library/cc223512.aspx
EXTRIGHTS_GUID_MAPPING = {
    "GetChanges": string_to_bin("1131f6aa-9c07-11d1-f79f-00c04fc2dcd2"),
    "GetChangesAll": string_to_bin("1131f6ad-9c07-11d1-f79f-00c04fc2dcd2"),
    "WriteMember": string_to_bin("bf9679c0-0de6-11d0-a285-00aa003049e2"),
    "UserForceChangePassword": string_to_bin("00299570-246d-11d0-a768-00aa006e0529"),
}

"""
objectClass mapping to GUID for some common classes (index is the ldapDisplayName).
Reference:
    https://msdn.microsoft.com/en-us/library/ms680938(v=vs.85).aspx
Can also be queried from the Schema.
This is included here instead of imported from impacket because of py2/py3 weirdness.
"""
OBJECTTYPE_GUID_MAP = {
    'group': 'bf967a9c-0de6-11d0-a285-00aa003049e2',
    'domain': '19195a5a-6da0-11d0-afd3-00c04fd930c9',
    'organizationalUnit': 'bf967aa5-0de6-11d0-a285-00aa003049e2',
    'user': 'bf967aba-0de6-11d0-a285-00aa003049e2',
    'groupPolicyContainer': 'f30e3bc2-9ff0-11d1-b603-0000f80367c1'
}

def parse_binary_acl(entry, entrytype, acl):
    """
    Main ACL structure parse function.
    This is offloaded to subprocesses and takes the current entry and the
    acl data as argument. This is then returned and processed back in the main process
    """
    if not acl:
        return entry, []
    sd = SecurityDescriptor(BytesIO(acl))
    relations = []
    # Parse owner
    osid = str(sd.owner_sid)
    ignoresids = ["S-1-3-0", "S-1-5-18"]
    # Ignore Creator Owner or Local System
    if osid not in ignoresids:
        relations.append(build_relation(osid, 'Owner'))
    for ace_object in sd.dacl.aces:
        if ace_object.ace.AceType != 0x05 and ace_object.ace.AceType != 0x00:
            # These are the only two aces we care about currently
            logging.debug('Don\'t care about acetype %d', ace_object.ace.AceType)
            continue
        # Check if sid is ignored
        sid = str(ace_object.acedata.sid)
        # Ignore Creator Owner or Local System
        if sid in ignoresids:
            continue
        if ace_object.ace.AceType == 0x05:
            # ACCESS_ALLOWED_OBJECT_ACE
            if not ace_object.has_flag(ACE.INHERITED_ACE) and ace_object.has_flag(ACE.INHERIT_ONLY_ACE):
                # ACE is set on this object, but only inherited, so not applicable to us
                continue

            # Check if the ACE has restrictions on object type (inherited case)
            if ace_object.has_flag(ACE.INHERITED_ACE) \
                and ace_object.acedata.has_flag(ACCESS_ALLOWED_OBJECT_ACE.ACE_INHERITED_OBJECT_TYPE_PRESENT):
                # Verify if the ACE applies to this object type
                if not ace_applies(ace_object.acedata.get_inherited_object_type().lower(), entrytype):
                    continue

            mask = ace_object.acedata.mask
            # Now the magic, we have to check all the rights BloodHound cares about

            # Check generic access masks first
            if mask.has_priv(ACCESS_MASK.GENERIC_ALL) or mask.has_priv(ACCESS_MASK.WRITE_DACL) \
                or mask.has_priv(ACCESS_MASK.WRITE_OWNER) or mask.has_priv(ACCESS_MASK.GENERIC_WRITE):
                # For all generic rights we should check if it applies to our object type
                if ace_object.acedata.has_flag(ACCESS_ALLOWED_OBJECT_ACE.ACE_OBJECT_TYPE_PRESENT) \
                    and not ace_applies(ace_object.acedata.get_object_type().lower(), entrytype):
                    # If it does not apply, break out of the loop here in order to
                    # avoid individual rights firing later on
                    continue
                # Check from high to low, ignore lower privs which may also match the bitmask,
                # even though this shouldn't happen since we check for exact matches currently
                if mask.has_priv(ACCESS_MASK.GENERIC_ALL):
                    relations.append(build_relation(sid, 'GenericAll'))
                    continue
                if mask.has_priv(ACCESS_MASK.GENERIC_WRITE):
                    relations.append(build_relation(sid, 'GenericWrite'))
                    # Don't skip this if it's the domain object, since BloodHound reports duplicate
                    # rights as well, and this might influence some queries
                    if entrytype != 'domain':
                        continue

                # These are specific bitmasks so don't break the loop from here
                if mask.has_priv(ACCESS_MASK.WRITE_DACL):
                    relations.append(build_relation(sid, 'WriteDacl'))

                if mask.has_priv(ACCESS_MASK.WRITE_OWNER):
                    relations.append(build_relation(sid, 'WriteOwner'))

            # Property write privileges
            writeprivs = ace_object.acedata.mask.has_priv(ACCESS_MASK.ADS_RIGHT_DS_WRITE_PROP)
            if writeprivs:
                # GenericWrite
                if entrytype in ['user', 'group'] and not ace_object.acedata.has_flag(ACCESS_ALLOWED_OBJECT_ACE.ACE_OBJECT_TYPE_PRESENT):
                    relations.append(build_relation(sid, 'GenericWrite'))
                if entrytype == 'group' and can_write_property(ace_object, EXTRIGHTS_GUID_MAPPING['WriteMember']):
                    relations.append(build_relation(sid, 'WriteProperty', 'AddMember'))

            # Extended rights
            control_access = ace_object.acedata.mask.has_priv(ACCESS_MASK.ADS_RIGHT_DS_CONTROL_ACCESS)
            if control_access:
                # All Extended
                if entrytype in ['user', 'domain'] and not ace_object.acedata.has_flag(ACCESS_ALLOWED_OBJECT_ACE.ACE_OBJECT_TYPE_PRESENT):
                    relations.append(build_relation(sid, 'ExtendedRight', 'All'))
                if entrytype == 'domain' and has_extended_right(ace_object, EXTRIGHTS_GUID_MAPPING['GetChanges']):
                    relations.append(build_relation(sid, 'ExtendedRight', 'GetChanges'))
                if entrytype == 'domain' and has_extended_right(ace_object, EXTRIGHTS_GUID_MAPPING['GetChangesAll']):
                    relations.append(build_relation(sid, 'ExtendedRight', 'GetChangesAll'))
                if entrytype == 'user' and has_extended_right(ace_object, EXTRIGHTS_GUID_MAPPING['UserForceChangePassword']):
                    relations.append(build_relation(sid, 'ExtendedRight', 'User-Force-Change-Password'))

            # print(ace_object.acedata.sid)
        if ace_object.ace.AceType == 0x00:
            mask = ace_object.acedata.mask
            # ACCESS_ALLOWED_ACE
            if mask.has_priv(ACCESS_MASK.GENERIC_ALL):
                # Generic all includes all other rights, so skip from here
                relations.append(build_relation(sid, 'GenericAll'))
                continue

            if mask.has_priv(ACCESS_MASK.GENERIC_WRITE):
                # Genericwrite is only for properties, don't skip after
                relations.append(build_relation(sid, 'GenericWrite'))

            if mask.has_priv(ACCESS_MASK.WRITE_OWNER):
                relations.append(build_relation(sid, 'WriteOwner'))

            # For users and domain, check extended rights
            if entrytype in ['user', 'domain'] and mask.has_priv(ACCESS_MASK.ADS_RIGHT_DS_CONTROL_ACCESS):
                relations.append(build_relation(sid, 'ExtendedRight', 'All'))

            if mask.has_priv(ACCESS_MASK.WRITE_DACL):
                relations.append(build_relation(sid, 'WriteDacl'))

    # pprint.pprint(entry)
        # pprint.pprint(relations)
    return entry, relations

def can_write_property(ace_object, binproperty):
    '''
    Checks if the access is sufficient to write to a specific property.
    This can either be because we have the right ADS_RIGHT_DS_WRITE_PROP and the correct GUID
    is set in ObjectType, or if we have the ADS_RIGHT_DS_WRITE_PROP right and the ObjectType
    is empty, in which case we can write to any property. This is documented in
    [MS-ADTS] section 5.1.3.2: https://msdn.microsoft.com/en-us/library/cc223511.aspx
    '''
    if not ace_object.acedata.mask.has_priv(ACCESS_MASK.ADS_RIGHT_DS_WRITE_PROP):
        return False
    if not ace_object.acedata.has_flag(ACCESS_ALLOWED_OBJECT_ACE.ACE_OBJECT_TYPE_PRESENT):
        # No ObjectType present - we have generic access on all properties
        return True
    else:
        # Both are binary here
        if ace_object.acedata.data.ObjectType == binproperty:
            return True
        else:
            return False

def has_extended_right(ace_object, binrightguid):
    '''
    Checks if the access is sufficient to control the right with the given GUID.
    This can either be because we have the right ADS_RIGHT_DS_CONTROL_ACCESS and the correct GUID
    is set in ObjectType, or if we have the ADS_RIGHT_DS_CONTROL_ACCESS right and the ObjectType
    is empty, in which case we have all extended rights. This is documented in
    [MS-ADTS] section 5.1.3.2: https://msdn.microsoft.com/en-us/library/cc223511.aspx
    '''
    if not ace_object.acedata.mask.has_priv(ACCESS_MASK.ADS_RIGHT_DS_CONTROL_ACCESS):
        return False
    if not ace_object.acedata.has_flag(ACCESS_ALLOWED_OBJECT_ACE.ACE_OBJECT_TYPE_PRESENT):
        # No ObjectType present - we have all extended rights
        return True
    else:
        # Both are binary here
        if ace_object.acedata.data.ObjectType == binrightguid:
            return True
        else:
            return False

def ace_applies(ace_guid, object_class):
    '''
    Checks if an ACE applies to this object (based on object classes).
    Note that this function assumes you already verified that InheritedObjectType is set (via the flag).
    If this is not set, the ACE applies to all object types.
    '''
    try:
        our_ace_guid = OBJECTTYPE_GUID_MAP[object_class]
    except KeyError:
        return False
    if ace_guid == our_ace_guid:
        return True
    # If none of these match, the ACE does not apply to this object
    return False

def build_relation(sid, relation, acetype=''):
    return {'rightname': relation, 'sid': sid, 'acetype': acetype}

class AclEnumerator(object):
    """
    Helper class for ACL parsing.
    """
    def __init__(self, addomain, addc, collect):
        self.addomain = addomain
        self.addc = addc
        # Store collection methods specified
        self.collect = collect
        self.pool = None

    def init_pool(self):
        self.pool = Pool()

"""
The following is Security Descriptor parsing using cstruct
Thanks to Erik Schamper for helping me implement this!
"""
cdef = native_str("""
struct SECURITY_DESCRIPTOR {
    uint8   Revision;
    uint8   Sbz1;
    uint16  Control;
    uint32  OffsetOwner;
    uint32  OffsetGroup;
    uint32  OffsetSacl;
    uint32  OffsetDacl;
};

struct LDAP_SID_IDENTIFIER_AUTHORITY {
    char    Value[6];
};

struct LDAP_SID {
    uint8   Revision;
    uint8   SubAuthorityCount;
    LDAP_SID_IDENTIFIER_AUTHORITY   IdentifierAuthority;
    uint32  SubAuthority[SubAuthorityCount];
};

struct ACL {
    uint8   AclRevision;
    uint8   Sbz1;
    uint16  AclSize;
    uint16  AceCount;
    uint16  Sbz2;
    char    Data[AclSize - 8];
};

struct ACE {
    uint8   AceType;
    uint8   AceFlags;
    uint16  AceSize;
    char    Data[AceSize - 4];
};

struct ACCESS_ALLOWED_ACE {
    uint32  Mask;
    LDAP_SID Sid;
};

struct ACCESS_ALLOWED_OBJECT_ACE {
    uint32  Mask;
    uint32  Flags;
    char    ObjectType[Flags & 1 * 16];
    char    InheritedObjectType[Flags & 2 * 8];
    LDAP_SID Sid;
};
""")
c_secd = cstruct()
c_secd.load(cdef, compiled=True)


class SecurityDescriptor(object):
    def __init__(self, fh):
        self.fh = fh
        self.descriptor = c_secd.SECURITY_DESCRIPTOR(fh)

        self.owner_sid = b''
        self.group_sid = b''
        self.sacl = b''
        self.dacl = b''

        if self.descriptor.OffsetOwner != 0:
            fh.seek(self.descriptor.OffsetOwner)
            self.owner_sid = LdapSid(fh=fh)

        if self.descriptor.OffsetGroup != 0:
            fh.seek(self.descriptor.OffsetGroup)
            self.group_sid = LdapSid(fh=fh)

        if self.descriptor.OffsetSacl != 0:
            fh.seek(self.descriptor.OffsetSacl)
            self.sacl = ACL(fh)

        if self.descriptor.OffsetDacl != 0:
            fh.seek(self.descriptor.OffsetDacl)
            self.dacl = ACL(fh)


class LdapSid(object):
    def __init__(self, fh=None, in_obj=None):
        if fh:
            self.fh = fh
            self.ldap_sid = c_secd.LDAP_SID(fh)
        else:
            self.ldap_sid = in_obj

    def __repr__(self):
        return "S-{}-{}-{}".format(self.ldap_sid.Revision, bytearray(self.ldap_sid.IdentifierAuthority.Value)[5], "-".join(['{:d}'.format(v) for v in self.ldap_sid.SubAuthority]))


class ACL(object):
    def __init__(self, fh):
        self.fh = fh
        self.acl = c_secd.ACL(fh)
        self.aces = []

        buf = BytesIO(self.acl.Data)
        for i in range(self.acl.AceCount):
            self.aces.append(ACE(buf))


class ACCESS_ALLOWED_ACE(object):
    def __init__(self, fh):
        self.fh = fh
        self.data = c_secd.ACCESS_ALLOWED_ACE(fh)
        self.sid = LdapSid(in_obj=self.data.Sid)
        self.mask = ACCESS_MASK(self.data.Mask)

    def __repr__(self):
        return "<ACCESS_ALLOWED_OBJECT_ACE Sid=%s Mask=%s>" % (str(self.sid), str(self.mask))

class ACCESS_DENIED_ACE(ACCESS_ALLOWED_ACE):
    pass


class ACCESS_ALLOWED_OBJECT_ACE(object):
    # Flag constants
    ACE_OBJECT_TYPE_PRESENT             = 0x01
    ACE_INHERITED_OBJECT_TYPE_PRESENT   = 0x02

    def __init__(self, fh):
        self.fh = fh
        self.data = c_secd.ACCESS_ALLOWED_OBJECT_ACE(fh)
        self.sid = LdapSid(in_obj=self.data.Sid)
        self.mask = ACCESS_MASK(self.data.Mask)

    def has_flag(self, flag):
        return self.data.Flags & flag == flag

    def get_object_type(self):
        if self.has_flag(self.ACE_OBJECT_TYPE_PRESENT):
            return bin_to_string(self.data.ObjectType)
        return None

    def get_inherited_object_type(self):
        if self.has_flag(self.ACE_INHERITED_OBJECT_TYPE_PRESENT):
            return bin_to_string(self.data.InheritedObjectType)
        return None

    def __repr__(self):
        out = []
        for name, value in iteritems(vars(ACCESS_ALLOWED_OBJECT_ACE)):
            if not name.startswith('_') and type(value) is int and self.has_flag(value):
                out.append(name)
        data = (' | '.join(out),
                str(self.sid),
                str(self.mask),
                self.get_object_type(),
                self.get_inherited_object_type())
        return "<ACCESS_ALLOWED_OBJECT_ACE Flags=%s Sid=%s \n\t\tMask=%s \n\t\tObjectType=%s InheritedObjectType=%s>" % data

class ACCESS_DENIED_OBJECT_ACE(ACCESS_ALLOWED_OBJECT_ACE):
    pass


"""
ACCESS_MASK as described in 2.4.3
https://msdn.microsoft.com/en-us/library/cc230294.aspx
"""
class ACCESS_MASK(object):
    # Flag constants

    # These constants are only used when WRITING
    # and are then translated into their actual rights
    SET_GENERIC_READ        = 0x80000000
    SET_GENERIC_WRITE       = 0x04000000
    SET_GENERIC_EXECUTE     = 0x20000000
    SET_GENERIC_ALL         = 0x10000000
    # When reading, these constants are actually represented by
    # the following for Active Directory specific Access Masks
    # Reference: https://docs.microsoft.com/en-us/dotnet/api/system.directoryservices.activedirectoryrights?view=netframework-4.7.2
    GENERIC_READ            = 0x00020094
    GENERIC_WRITE           = 0x00020028
    GENERIC_EXECUTE         = 0x00020004
    GENERIC_ALL             = 0x000F01FF

    # These are actual rights (for all ACE types)
    MAXIMUM_ALLOWED         = 0x02000000
    ACCESS_SYSTEM_SECURITY  = 0x01000000
    SYNCHRONIZE             = 0x00100000
    WRITE_OWNER             = 0x00080000
    WRITE_DACL              = 0x00040000
    READ_CONTROL            = 0x00020000
    DELETE                  = 0x00010000

    # ACE type specific mask constants (for ACCESS_ALLOWED_OBJECT_ACE)
    # Note that while not documented, these also seem valid
    # for ACCESS_ALLOWED_ACE types
    ADS_RIGHT_DS_CONTROL_ACCESS         = 0x00000100
    ADS_RIGHT_DS_CREATE_CHILD           = 0x00000001
    ADS_RIGHT_DS_DELETE_CHILD           = 0x00000002
    ADS_RIGHT_DS_READ_PROP              = 0x00000010
    ADS_RIGHT_DS_WRITE_PROP             = 0x00000020
    ADS_RIGHT_DS_SELF                   = 0x00000008

    def __init__(self, mask):
        self.mask = mask

    def has_priv(self, priv):
        return self.mask & priv == priv

    def set_priv(self, priv):
        self.mask |= priv

    def remove_priv(self, priv):
        self.mask ^= priv

    def __repr__(self):
        out = []
        for name, value in iteritems(vars(ACCESS_MASK)):
            if not name.startswith('_') and type(value) is int and self.has_priv(value):
                out.append(name)
        return "<ACCESS_MASK RawMask=%d Flags=%s>" % (self.mask, ' | '.join(out))



class ACE(object):
    CONTAINER_INHERIT_ACE       = 0x02
    FAILED_ACCESS_ACE_FLAG      = 0x80
    INHERIT_ONLY_ACE            = 0x08
    INHERITED_ACE               = 0x10
    NO_PROPAGATE_INHERIT_ACE    = 0x04
    OBJECT_INHERIT_ACE          = 0x01
    SUCCESSFUL_ACCESS_ACE_FLAG  = 0x04

    def __init__(self, fh):
        self.fh = fh
        self.ace = c_secd.ACE(fh)
        self.acedata = None
        buf = BytesIO(self.ace.Data)
        if self.ace.AceType == 0x00:
            # ACCESS_ALLOWED_ACE
            self.acedata = ACCESS_ALLOWED_ACE(buf)
        elif self.ace.AceType == 0x05:
            # ACCESS_ALLOWED_OBJECT_ACE
            self.acedata = ACCESS_ALLOWED_OBJECT_ACE(buf)
        elif self.ace.AceType == 0x01:
            # ACCESS_DENIED_ACE
            self.acedata = ACCESS_DENIED_ACE(buf)
        elif self.ace.AceType == 0x06:
            # ACCESS_DENIED_OBJECT_ACE
            self.acedata = ACCESS_DENIED_OBJECT_ACE(buf)
        # else:
        #     print 'Unsupported type %d' % self.ace.AceType

        if self.acedata:
            self.mask = ACCESS_MASK(self.acedata.data.Mask)

    def __repr__(self):
        out = []
        for name, value in iteritems(vars(ACE)):
            if not name.startswith('_') and type(value) is int and self.has_flag(value):
                out.append(name)
        return "<ACE Type=%s Flags=%s RawFlags=%d \n\tAce=%s>" % (self.ace.AceType, ' | '.join(out), self.ace.AceFlags, str(self.acedata))

    def has_flag(self, flag):
        return self.ace.AceFlags & flag == flag
