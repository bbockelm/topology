
from collections import defaultdict
import re
import ldap
ldap.set_option(ldap.OPT_TIMEOUT, 10)
ldap.set_option(ldap.OPT_NETWORK_TIMEOUT, 10)
import asn1
import hashlib

__oid_map = {
   "DC": "0.9.2342.19200300.100.1.25",
   "OU": "2.5.4.11",
   "CN": "2.5.4.3",
   "O": "2.5.4.10",
   "ST": "2.5.4.8",
   "C": "2.5.4.6",
   "L": "2.5.4.7",
   "postalCode": "2.5.4.17",
   "street": "2.5.4.9",
   "emailAddress": "1.2.840.113549.1.9.1",
   }


__dn_split_re = re.compile("/([A-Za-z]+)=")


class DataError(Exception):
    """Raised when there is a problem in the topology or VO data"""


def _generate_ligo_dns():
    """
    Query the LIGO LDAP server for all grid DNs in the LVC collab.

    Returns a list of DNs.
    """
    ldap_obj = ldap.initialize("ldaps://ldap.ligo.org")
    query = "(&(isMemberOf=Communities:LSCVirgoLIGOGroupMembers)(gridX509subject=*))"
    results = ldap_obj.search_s("ou=people,dc=ligo,dc=org", ldap.SCOPE_ONELEVEL,
                                query, ["gridX509subject"])
    all_dns = []
    for result in results:
        user_dns = result[1].get('gridX509subject', [])
        for dn in user_dns:
            if dn.startswith(b"/"):
                all_dns.append(dn.replace(b"\n", b" ").decode("utf-8"))

    return all_dns


def _generate_dn_hash(dn: str):
    """
    Given a DN one-liner as commonly encoded in the grid world
    (e.g., output of `openssl x509 -in $FILE -noout -subject`), run
    the OpenSSL subject hash generation algorithm.

    This is done by calculating the SHA-1 sum of the canonical form of the
    X509 certificate's subject.  Formatting is a bit like this:

    SEQUENCE:
       SET:
         SEQUENCE:
           OID
           UTF8String

    All the UTF-8 values should be converted to lower-case and multiple
    spaces should be replaced with a single space.  That is, "Foo  Bar"
    should be substituted with "foo bar" for the canonical form.
    """
    encoder = asn1.Encoder()
    encoder.start()
    info = __dn_split_re.split(dn)[1:]
    for attr, val in zip(info[0::2], info[1::2]):
        oid = __oid_map.get(attr)
        if not oid:
            raise ValueError("OID for attribute {} is not known.".format(attr))
        encoder.enter(0x11)
        encoder.enter(0x10)
        encoder.write(oid, 0x06)
        encoder.write(val.lower().encode("utf-8"), 0x0c)
        encoder.leave()
        encoder.leave()
    output = encoder.output()
    hash_obj = hashlib.sha1()
    hash_obj.update(output)
    digest = hash_obj.digest()
    int_summary = digest[0] | digest[1] << 8 | digest[2] << 16 | digest[3] << 24
    return "%08lx.0" % int_summary


def _get_resource_by_fqdn(fqdn, resource_groups):
    for group in resource_groups:
        for resource in group.resources:
            if fqdn.lower() == resource.fqdn.lower():
                return resource


def _get_cache_resource(fqdn, resource_groups, suppress_errors):
    resource = None
    if fqdn:
        resource = _get_resource_by_fqdn(fqdn, resource_groups)
        if not resource:
            if suppress_errors:
                return None
            else:
                raise ValueError("No resource registered for FQDN {}".format(fqdn))
        if "XRootD cache server" not in resource.service_names:
            if suppress_errors:
                return None
            else:
                raise ValueError("Resource {} (FQDN {}) is not a cache service.".format(resource.name, fqdn))
    return resource


def _cache_is_allowed(resource, vo_name, stashcache_data, public, suppress_errors):
    allowed_vos = resource.data.get("AllowedVOs")
    if allowed_vos is None:
        if suppress_errors:
            return False
        else:
            raise ValueError("Cache server {} (FQDN {}) does not provide an AllowedVOs list.".format(resource.name, resource.fqdn))

    matches_cache = False
    for vo in allowed_vos:
        if vo == 'ANY' or vo == vo_name or (public and vo == 'PUBLIC'):
            matches_cache = True
            break
    if not matches_cache:
        return False

    # For public data, caching is one-way: we OK things as long as the
    # cache is interested in the data.
    if public:
      return True

    allowed_caches = stashcache_data.get("AllowedCaches")
    if allowed_caches is None:
        if suppress_errors:
            return False
        else:
            raise ValueError("VO {} in StashCache does not provide an AllowedCaches list.".format(vo_name))
    for cache_name in allowed_caches:
        if cache_name == 'ANY' or cache_name == resource.name:
            return True
    return False


def generate_authfile(vo_data, resource_groups, fqdn=None, legacy=True, suppress_errors=True):
    """
    Generate the Xrootd authfile needed by a StashCache cache server.
    """
    authfile = ""
    id_to_dir = defaultdict(list)

    resource = _get_cache_resource(fqdn, resource_groups, suppress_errors)
    if fqdn and not resource:
        return ""

    for vo_name, vo_data in vo_data.vos.items():
        stashcache_data = vo_data.get('DataFederations', {}).get('StashCache')
        if not stashcache_data:
            continue

        has_non_public = False
        for authz_list in stashcache_data.get("Namespaces", {}).values():
            for authz in authz_list:
                if authz != "PUBLIC":
                    has_non_public = True
                    break
        if not has_non_public:
            continue

        if resource and not _cache_is_allowed(resource, vo_name, stashcache_data, False, suppress_errors):
            continue

        for dirname, authz_list in stashcache_data.get("Namespaces", {}).items():
            for authz in authz_list:
                if not isinstance(authz, str):
                    continue
                if authz.startswith("FQAN:"):
                    id_to_dir["g {}".format(authz[5:])].append(dirname)
                elif authz.startswith("DN:"):
                    hash = _generate_dn_hash(authz[3:])
                    id_to_dir["u {}".format(hash)].append(dirname)

    if legacy:
        for dn in _generate_ligo_dns():
            hash = _generate_dn_hash(dn)
            id_to_dir["u {}".format(hash)].append("/user/ligo")

    for id, dir_list in id_to_dir.items():
        if dir_list:
            authfile += "{} {}\n".format(id,
                " ".join([i + " rl" for i in dir_list]))

    return authfile


def generate_public_authfile(vo_data, resource_groups, fqdn=None, legacy=True, suppress_errors=True):
    """
    Generate the Xrootd authfile needed for public caches
    """
    if legacy:
        authfile = "u * /user/ligo -rl \\\n"
    else:
        authfile = "u * \\\n"
    id_to_dir = defaultdict(list)

    resource = _get_cache_resource(fqdn, resource_groups, suppress_errors)
    if fqdn and not resource:
        return ""

    public_dirs = [] 
    for vo_name, vo_data in vo_data.vos.items():
        stashcache_data = vo_data.get('DataFederations', {}).get('StashCache')
        if not stashcache_data:
            continue
        if resource and not _cache_is_allowed(resource, vo_name, stashcache_data, True, suppress_errors):
            continue

        for dirname, authz_list in stashcache_data.get("Namespaces", {}).items():
            for authz in authz_list:
                if authz == "PUBLIC":
                    public_dirs.append(dirname)

    for dirname in public_dirs:
        authfile += "    {} rl \\\n".format(dirname)

    if authfile.endswith("\\\n"):
        authfile = authfile[:-2] + "\n"

    return authfile


def generate_cache_scitokens(fqdn, vo_data, resource_groups, suppress_errors=True):
    """
    Generate the SciTokens needed by a StashCache cache server.
    """
    scitokens_cfg = "[Global]\n"
    id_to_dir = defaultdict(list)

    resource = _get_cache_resource(fqdn, resource_groups, suppress_errors)
    if fqdn and not resource:
        return ""

    scitokens_cfg += "audience = {}, https://{}\n\n".format(resource.name, fqdn)

    for vo_name, vo_data in vo_data.vos.items():
        stashcache_data = vo_data.get('DataFederations', {}).get('StashCache')
        if not stashcache_data:
            continue

        has_non_public = False
        for authz_list in stashcache_data.get("Namespaces", {}).values():
            for authz in authz_list:
                if authz != "PUBLIC":
                    has_non_public = True
                    break
        if not has_non_public:
            continue

        if resource and not _cache_is_allowed(resource, vo_name, stashcache_data, False, suppress_errors):
            continue

        for dirname, authz_list in stashcache_data.get("Namespaces", {}).items():
            for authz in authz_list:
                if not isinstance(authz, dict) or not 'SciTokens' in authz or not isinstance(authz['SciTokens'], dict):
                    continue
                if 'Base Path' not in authz['SciTokens']:
                    raise DataError("'Base Path' missing from the SciTokens config for {}.".format(vo_name))
                if 'Issuer' not in authz['SciTokens']:
                    raise DataError("'Issuer' missing from the SciTokens config for {}.".format(vo_name))
                scitokens_cfg += "[Issuer {}]\n".format(dirname)
                scitokens_cfg += "issuer = {}\n".format(authz['SciTokens']['Issuer'])
                scitokens_cfg += "base_path = {}\n".format(authz['SciTokens']['Base Path'])
                if 'Restricted Path' in authz['SciTokens']:
                     scitokens_cfg += "restricted_path = {}\n".format(authz['SciTokens']['Restricted Path'])
                scitokens_cfg += "\n"

    return scitokens_cfg


def _origin_is_allowed(origin_hostname, vo_name, stashcache_data, resource_groups, suppress_errors=True):
    origin_resource = _get_resource_by_fqdn(origin_hostname, resource_groups)
    if not origin_resource:
        if suppress_errors:
            return False
        else:
            raise DataError("FQDN {} is not a registered service.".format(origin_hostname))
    if 'XRootD origin server' not in origin_resource.service_names:
        if suppress_errors:
            return False
        else:
            raise DataError("FQDN {} (resource name {}) does not provide an origin service.".format(origin_hostname, origin_resource.name))
    allowed_vos = origin_resource.data.get("AllowedVOs")
    if allowed_vos is None:
        if suppress_errors:
            return False
        else:
            raise DataError("Origin server at {} (resource name {}) does not provide an AllowedVOs list.".format(origin_hostname, origin_resource.name))

    matches_origin = False
    for vo in allowed_vos:
        if vo == 'ANY' or vo == vo_name:
            matches_origin = True
            break
    if not matches_origin:
        return False

    allowed_origins = stashcache_data.get("AllowedOrigins")
    if allowed_origins is None:
        if suppress_errors:
            return False
        else:
            raise DataError("VO {} in StashCache does not provide an AllowedOrigins list.".format(vo_name))
    for origin_name in allowed_origins:
        if origin_name == origin_resource.name:
            return True
    return False

def _get_allowed_caches(vo_name, stashcache_data, resource_groups, suppress_errors=True):
    allowed_caches = stashcache_data.get("AllowedCaches")
    if allowed_caches is None:
        if suppress_errors:
            return []
        else:
            raise DataError("VO {} enables StashCache but does not specify the allowed caches.".format(vo_name))

    resources = []
    for group in resource_groups:
        for resource in group.resources:
            # First, does this provide a cache service?
            if 'XRootD cache server' not in resource.service_names:
                continue

            # Next, does it allow this VO?  Unlike the StashCache origin case requiring the origin to list AllowedVOs,
            # we do not consider the lack of AllowedVOs an error as the cache doesn't
            # explicitly record *which* data federation it is participating in (might not be SC!).
            matches_vo = False
            for vo in resource.data.get("AllowedVOs", []):
                if vo == 'ANY':
                    matches_vo = True
                    break
                elif vo == 'PUBLIC':
                    continue
                elif vo == vo_name:
                    matches_vo = True
                    break
            if not matches_vo:
                continue
            matches_resource = False
            for cache in allowed_caches:
                if cache == 'ANY':
                    matches_resource = True
                    break
                elif cache == resource.name:
                    matches_resource = True
                    break
            if not matches_resource:
                continue
            resources.append(resource)
    return resources


def generate_origin_authfile(origin_hostname, vo_data, resource_groups, suppress_errors=True, public_only=False):

    public_namespaces = []
    id_to_namespaces = defaultdict(list)
    for vo_name, vo_data in vo_data.vos.items():
        stashcache_data = vo_data.get('DataFederations', {}).get('StashCache')
        if not stashcache_data:
            continue

        if not _origin_is_allowed(origin_hostname, vo_name, stashcache_data, resource_groups, suppress_errors=suppress_errors):
            continue

        for namespace, authz_list in stashcache_data.get("Namespaces", {}).items():
            all_public = True
            for entry in authz_list:
                if entry != "PUBLIC":
                    all_public = False
                    break
            if all_public:
                public_namespaces.append(namespace)
                continue

            if public_only:
                continue

            allowed_caches = stashcache_data.get("AllowedCaches")
            if allowed_caches is None:
                if suppress_errors:
                    continue
                else:
                    raise DataError("VO {} enables StashCache but does not specify the allowed caches.".format(vo_name))

            for resource in _get_allowed_caches(vo_name, stashcache_data, resource_groups, suppress_errors=suppress_errors):
                dn = resource.data.get("DN")
                if not dn:
                    if suppress_errors:
                        continue
                    else:
                        raise DataError("Resource {} is an allowed cache for VO {} but does not provide a DN.".format(resource.name, vo_name))
                dn_hash = _generate_dn_hash(dn)
                id_to_namespaces[dn_hash].append(namespace)

    results = ""
    for id, namespaces in id_to_namespaces.items():
        results += "u {} {}\n".format(id, " ".join("{} lr".format(i) for i in namespaces))
    if public_namespaces:
        results += "\nu * {}\n".format(" ".join("{} lr".format(i) for i in public_namespaces))
    return results
