import json
import time
import hmac
import hashlib
import base64
import secrets
import string
import requests
import copy
from urllib.parse import urlparse

# =========================
# CONFIG
# =========================

ACCESS_KEY = "YOUR_ACCESS_KEY"
SECRET_KEY = "YOUR_SECRET_KEY"

BASE_URL = "https://cad.onshape.com"

TEMPLATE_DID = "8e8b3998215a0322f077377f"
TEMPLATE_WID = "5f8d3c86370676fed6404463"
TEMPLATE_EID = "9acb704becbc1624ad3acf6a"  # Part Studio

FEATURE_NAME = "Die Engrave From JSON 1"

# =========================
# AUTH (Onshape HMAC)
# =========================


def _make_nonce(length=25):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))

def _make_headers(method, path, query="", content_type="application/json"):
    nonce = _make_nonce()
    date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())

    string_to_sign = (
        f"{method}\n"
        f"{nonce}\n"
        f"{date}\n"
        f"{content_type}\n"
        f"{path}\n"
        f"{query}\n"
    ).lower()

    signature = base64.b64encode(
        hmac.new(
            SECRET_KEY.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")

    return {
        "Authorization": f"On {ACCESS_KEY}:HmacSHA256:{signature}",
        "On-Nonce": nonce,
        "Date": date,
        "Content-Type": content_type,
        "Accept": "application/json",
    }

def _request(method, path, json_body=None, query=""):
    url = BASE_URL + path + query
    headers = _make_headers(method, path, query=query)

    r = requests.request(method, url, headers=headers, json=json_body, allow_redirects=False)

    if r.status_code == 307:
        from urllib.parse import urlparse

        redirect_url = r.headers["Location"]
        parsed = urlparse(redirect_url)
        redirect_headers = _make_headers(method, parsed.path, query=parsed.query)

        r = requests.request(
            method,
            redirect_url,
            headers=redirect_headers,
            json=json_body,
            allow_redirects=False,
        )

    if not r.ok:
        print("STATUS:", r.status_code)
        print("URL:", url)
        print("BODY SENT:", json_body)
        print("RESPONSE:", r.text)
        raise Exception(f"Request failed: {r.status_code}")

    if not r.text:
        return None

    content_type = r.headers.get("Content-Type", "")
    if "application/json" in content_type:
        return r.json()

    return r.text


# =========================
# STEP 1: COPY TEMPLATE
# =========================

def copy_template():
    path = f"/api/documents/{TEMPLATE_DID}/workspaces/{TEMPLATE_WID}/copy"

    body = {
        "newName": f"Auto Die Build {int(time.time())}",
        "isPublic": True
    }

    result = _request("POST", path, body)

    print("Copy result:", result)

    new_did = result["newDocumentId"]
    new_wid = result["newWorkspaceId"]

    return new_did, new_wid
    

# =========================
# STEP 2: GET FEATURES/ELEMENTS
# =========================

def get_elements(did, wid):
    path = f"/api/documents/d/{did}/w/{wid}/elements"
    return _request("GET", path)

def find_partstudio_element_id(did, wid, preferred_name=None):
    elements = get_elements(did, wid)

    # First try exact name match, if you know the Part Studio tab name
    if preferred_name is not None:
        for el in elements:
            if el.get("name") == preferred_name and el.get("elementType") == "PARTSTUDIO":
                return el["id"]

    # Otherwise take the first Part Studio
    for el in elements:
        if el.get("elementType") == "PARTSTUDIO":
            return el["id"]

    raise Exception("No Part Studio element found in copied document")

def get_features(did, wid, eid):
    path = f"/api/partstudios/d/{did}/w/{wid}/e/{eid}/features"
    return _request("GET", path)


# =========================
# STEP 3: FIND TARGET FEATURE
# =========================

def find_feature_by_name(features_json, feature_name):
    for feat in features_json["features"]:
        msg = feat.get("message", {})
        if msg.get("name") == feature_name:
            return feat
    raise Exception(f"Feature not found: {feature_name}")


def find_feature(features_json):
    return find_feature_by_name(features_json, FEATURE_NAME)


# =========================
# STEP 4: UPDATE JSON PARAM
# =========================



def update_feature(did, wid, eid, feature, json_data, features_json):
    feature_id = feature["message"]["featureId"]
    feature_to_send = copy.deepcopy(feature)

    found_param = False
    for param in feature_to_send["message"]["parameters"]:
        pmsg = param.get("message", {})
        if pmsg.get("parameterId") == "engraveData":
            print("OLD engraveData param:")
            print(json.dumps(param, indent=2)[:4000])

            param["type"] = 149  # BTMParameterString
            param["typeName"] = "BTMParameterString"
            pmsg["value"] = json.dumps(json_data)

            print("NEW engraveData param:")
            print(json.dumps(param, indent=2)[:4000])

            found_param = True
            break

    if not found_param:
        raise Exception("Could not find parameterId='engraveData'")

    payload = {
        "feature": feature_to_send,
        "serializationVersion": features_json["serializationVersion"],
        "sourceMicroversion": features_json["sourceMicroversion"],
    }

    if "libraryVersion" in features_json:
        payload["libraryVersion"] = features_json["libraryVersion"]

    path = f"/api/partstudios/d/{did}/w/{wid}/e/{eid}/features/featureid/{feature_id}"
    _request("POST", path, payload)


# =========================
# STEP 5: WAIT FOR REGEN
# =========================

def wait_for_feature_regen(did, wid, eid, feature_name, timeout=60, interval=2.0):
    deadline = time.time() + timeout
    last_feature = None

    while time.time() < deadline:
        features = get_features(did, wid, eid)
        feature = find_feature_by_name(features, feature_name)
        last_feature = feature

        msg = feature.get("message", {})
        status = msg.get("featureStatus")
        state = msg.get("featureState")

        print("featureStatus:", status, "featureState:", state)

        # Dump full message while debugging
        print(json.dumps(msg, indent=2)[:8000])

        # If Onshape exposes an error, stop immediately
        if status in ("FAILURE", "ERROR") or state in ("FAILURE", "ERROR"):
            raise Exception(f"Feature regeneration failed: status={status}, state={state}")

        # Many successful features come back with OK-ish/clean states.
        # Since the exact enums vary, accept anything non-error after a few polls
        # only if the feature exists and does not report failure.
        if status not in ("FAILURE", "ERROR") and state not in ("FAILURE", "ERROR"):
            return feature

        time.sleep(interval)

    raise TimeoutError(f"Timed out waiting for feature regeneration. Last feature:\n{json.dumps(last_feature, indent=2)[:8000]}")


# =========================
# STEP 6: EXPORT STL
# =========================



def export_stl(did, wid, eid):
    path = f"/api/partstudios/d/{did}/w/{wid}/e/{eid}/stl"
    query = "mode=binary&grouping=true"

    url = BASE_URL + path + "?" + query
    headers = _make_headers("GET", path, query=query, content_type="application/json")

    # Do NOT auto-follow. Onshape sync export returns 307.
    r = requests.get(url, headers=headers, allow_redirects=False)

    if r.status_code == 307:
        redirect_url = r.headers["Location"]
        parsed = urlparse(redirect_url)

        redirect_query = parsed.query  # no leading '?'
        redirect_headers = _make_headers(
            "GET",
            parsed.path,
            query=redirect_query,
            content_type="application/json"
        )

        r = requests.get(
            redirect_url,
            headers=redirect_headers,
            allow_redirects=False
        )

    if not r.ok:
        print("STATUS:", r.status_code)
        print("URL:", url)
        print("RESPONSE:", r.text)
        raise Exception("Export failed")

    return r.content


# =========================
# MAIN ENTRY
# =========================

def build_die_from_json(json_path, output_file="die.stl"):
    with open(json_path, "r") as f:
        json_data = json.load(f)

    print("Copying template...")
    did, wid = copy_template()

    print("Finding copied Part Studio...")
    eid = find_partstudio_element_id(did, wid)
    print("Copied EID:", eid)

    print("Fetching features...")
    features = get_features(did, wid, eid)

    print("Finding feature...")
    target = find_feature(features)

    print("Updating JSON...")
    update_feature(did, wid, eid, target, json_data, features)

    print("Waiting for regen...")
    target_after = wait_for_feature_regen(did, wid, eid, FEATURE_NAME)

    print("Features After Regen: ")
    print(json.dumps(target_after, indent=2)[:8000])

    print("Exporting STL...")
    stl_data = export_stl(did, wid, eid)

    with open(output_file, "wb") as f:
        f.write(stl_data)

    print("Done:", output_file)