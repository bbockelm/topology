import pprint
import xmltodict
import yaml

from typing import Dict, List, Union
from convertlib import is_null, simplify_attr_list, ensure_list

with open('vos.xml', 'r') as vo_xml_file:
    # Use dict_constructore = dict so we don't get ordered dicts, we don't really care about ordering
    parsed = xmltodict.parse(vo_xml_file.read(), dict_constructor=dict)


def is_true_str(a_str: Union[str, None]) -> bool:
    return a_str and a_str.strip("'\" ").lower() in ["1", "on", "true"]


# Multiline string to look nice'er
def str_presenter(dumper, data):
    if len(data.splitlines()) > 1:  # check for multiline string
        return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')
    return dumper.represent_scalar('tag:yaml.org,2002:str', data)

yaml.add_representer(str, str_presenter)


def simplify_contacttypes(contacttypes):
    """Simplify ContactTypes attribute

    Turn e.g.
    {"ContactType":
        [{"Contacts":
            {"Contact": [{"Name": "Steve Timm"},
                         {"Name": "Joe Boyd"}]},
          "Type": "Miscellaneous Contact"}
        ]
    }

    into

    {"Miscellanous Contact":
        [ "Steve Timm", "Joe Boyd" ]
    }
    """
    if is_null(contacttypes, "ContactType"):
        return None

    new_contacttypes = {}
    for ct in ensure_list(contacttypes["ContactType"]):
        if is_null(ct, "Contacts", "Contact"):
            continue
        type_ = ct["Type"]
        # Remove duplicates but keep ordering
        contacts = []
        for c in ensure_list(ct["Contacts"]["Contact"]):
            if c["Name"] not in contacts:
                contacts.append(c["Name"])
        new_contacttypes[type_] = contacts

    return new_contacttypes


def simplify_reportinggroups(reportinggroups):
    """Simplify ReportingGroups attributes

    Turn e.g.
    {"ReportingGroup": [{"Contacts": {"Contact": [{"Name": "a"},
                                                  {"Name": "b"}
                                     },
                         "FQANs": {"FQAN": [{"GroupName": "XXX",
                                             "Role": "YYY"}]
                                  }
                         "Name": "ZZZ"
                        }]
    }

    into
    {"ZZZ": {"Contacts": ["a", "b"],
             "FQANs": [{"GroupName": "XXX", "Role": "YYY"}]
            }
    }

    """
    if is_null(reportinggroups, "ReportingGroup"):
        return None

    # [{"Name": "XXX", <...>}, {"Name": "YYY", <...>}]  becomes
    #  {"XXX": {<...>}, "YYY": {<...>}>
    new_reportinggroups = simplify_attr_list(reportinggroups["ReportingGroup"], "Name")
    if not new_reportinggroups:  # only null entries found
        return None

    for rgname, rgdata in new_reportinggroups.items():
        if not is_null(rgdata["Contacts"], "Contact"):
            # {"Contacts": {"Contact": [{"Name": "a"}, {"Name": "b"}]}} becomes
            # {"Contacts": ["a", "b"]}
            new_contacts = []
            for c in ensure_list(rgdata["Contacts"]["Contact"]):
                if not is_null(c, "Name") and c["Name"] not in new_contacts:
                    new_contacts.append(c["Name"])
            rgdata["Contacts"] = new_contacts

        if not is_null(rgdata["FQANs"], "FQAN"):
            rgdata["FQANs"] = ensure_list(rgdata["FQANs"]["FQAN"])

    return new_reportinggroups


def simplify_oasis_managers(managers):
    """Simplify OASIS/Managers attributes

    Turn
    {"Manager": [{"Name": "a", "DNs": {"DN": [...]}}]}
    into
    {"a": {"DNs": [...]}}
    """
    if is_null(managers, "Manager"):
        return None
    new_managers = simplify_attr_list(managers["Manager"], "Name")
    for manager, data in new_managers.items():
        if not is_null(data, "DNs"):
            data["DNs"] = data["DNs"]["DN"]
        if not is_null(data, "ContactID"):
            data["ContactID"] = int(data["ContactID"])
    return new_managers


def simplify_fields_of_science(fos: Dict) -> Union[Dict, None]:
    """Turn
    {"PrimaryFields": {"Field": ["P1", "P2", ...]},
     "SecondaryFields": {"Field": ["S1", "S2", ...]}}
    into
    {"PrimaryFields": ["P1", "P2", ...],
     "SecondaryFields": ["S1", "S2", ...]}
    """
    if is_null(fos, "PrimaryFields") or is_null(fos["PrimaryFields"], "Field"):
        return None
    new_fields = {"PrimaryFields": ensure_list(fos["PrimaryFields"]["Field"])}
    if not is_null(fos, "SecondaryFields", "Field"):
        new_fields["SecondaryFields"] = ensure_list(fos["SecondaryFields"]["Field"])
    return new_fields


reportinggroup_data = {}

for vo in parsed['VOSummary']['VO']:
    name = vo["Name"]
    if "/" in name: continue  # bad name

    if "ID" in vo:
        vo["ID"] = int(vo["ID"])
    vo["Active"] = is_true_str(vo.get("Active", ""))
    vo["CertificateOnly"] = is_true_str(vo.get("CertificateOnly", ""))
    vo["Disable"] = is_true_str(vo.get("Disable", ""))
    if "ContactTypes" in vo:
        vo["Contacts"] = simplify_contacttypes(vo["ContactTypes"])
        del vo["ContactTypes"]
    if "ReportingGroups" in vo:
        rgs = simplify_reportinggroups(vo["ReportingGroups"])
        if rgs is not None:
            vo["ReportingGroups"] = sorted(set(rgs.keys()))
            reportinggroup_data.update(rgs)
    if "OASIS" in vo:
        if not is_null(vo["OASIS"], "Managers"):
            vo["OASIS"]["Managers"] = simplify_oasis_managers(vo["OASIS"]["Managers"])
        else:
            vo["OASIS"].pop("Managers", None)
        if not is_null(vo["OASIS"], "OASISRepoURLs", "URL"):
            vo["OASIS"]["OASISRepoURLs"] = ensure_list(vo["OASIS"]["OASISRepoURLs"]["URL"])
        else:
            vo["OASIS"].pop("OASISRepoURLs")
        vo["OASIS"]["UseOASIS"] = is_true_str(vo["OASIS"].get("UseOASIS", ""))
    if not is_null(vo, "FieldsOfScience"):
        vo["FieldsOfScience"] = simplify_fields_of_science(vo["FieldsOfScience"])
    if not is_null(vo, "ParentVO"):
        vo["ParentVO"]["ID"] = int(vo["ParentVO"]["ID"])
    vo.pop("MemeberResources", None)  # will recreate MemeberResources [sic] from RG data

    # delete empty fields
    for key in ["Contacts", "MembershipServicesURL", "ParentVO", "PrimaryURL", "PurposeURL", "ReportingGroups", "SupportURL"]:
        if is_null(vo, key):
            vo.pop(key, None)

    serialized = yaml.safe_dump(vo, encoding='utf-8', default_flow_style=False)
    print(serialized.decode())
    with open("virtual-organizations/{0}.yaml".format(name), 'w') as f:
        f.write(serialized.decode())

with open("virtual-organizations/REPORTING_GROUPS.yaml", "w") as f:
    f.write(yaml.safe_dump(reportinggroup_data, encoding="utf-8").decode())
