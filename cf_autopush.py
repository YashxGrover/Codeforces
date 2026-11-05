"""
Codeforces Auto-Pusher: Push accepted solutions to GitHub.
Rated -> Codeforces/<rating>/<contestId><index>_<name>.ext
Unrated -> Codeforces/Contests/<contest_name>_<id>/<index>_<name>.ext
"""

import base64
import hashlib
import json
import os
import random
import re
import string
import sys
import time
from typing import Dict, List, Optional, Set

import requests
from github import Auth, Github, GithubException, InputGitTreeElement

# -- Config --

CF_API_KEY = os.environ.get("CF_API_KEY", "")
CF_API_SECRET = os.environ.get("CF_API_SECRET", "")
CF_HANDLE = os.environ.get("CF_HANDLE", "")
GH_PAT = os.environ.get("GH_PAT", "")
TARGET_REPO = os.environ.get("TARGET_REPO", "")
TARGET_DIR = os.environ.get("TARGET_DIR", "Codeforces")

CF_API_BASE = "https://codeforces.com/api"
STATE_FILE = f"{TARGET_DIR}/pushed.json" if TARGET_DIR else "pushed.json"
BATCH_SIZE = 50

# -- Language -> extension --

LANG_EXT_MAP = {
    "GNU C": ".c", "GNU C11": ".c", "GNU C17": ".c", "C": ".c",
    "GNU C++": ".cpp", "GNU C++0x": ".cpp", "GNU C++11": ".cpp",
    "GNU C++14": ".cpp", "GNU C++17": ".cpp", "GNU C++17 (64)": ".cpp",
    "GNU C++20 (64)": ".cpp", "GNU C++23 (64)": ".cpp",
    "C++17 (GCC 7-32)": ".cpp", "C++20 (GCC 13-64)": ".cpp",
    "C++23 (GCC 14-64, msys2)": ".cpp",
    "Clang++17 Diagnostics": ".cpp", "Clang++20 Diagnostics": ".cpp",
    "Microsoft Visual C++ 2017": ".cpp", "Microsoft Visual C++ 2010": ".cpp",
    "C# 8": ".cs", "C# 10": ".cs", "C# Mono": ".cs", "Mono C#": ".cs",
    "D": ".d", "Go": ".go", "Haskell": ".hs",
    "Java 8": ".java", "Java 11": ".java", "Java 17": ".java", "Java 21": ".java",
    "Kotlin 1.4": ".kt", "Kotlin 1.5": ".kt", "Kotlin 1.6": ".kt",
    "Kotlin 1.7": ".kt", "Kotlin 1.9": ".kt",
    "OCaml": ".ml", "Delphi": ".pas", "FPC": ".pas", "Pascal": ".pas",
    "Perl": ".pl", "PHP": ".php",
    "Python 2": ".py", "Python 3": ".py", "PyPy 2": ".py", "PyPy 2-64": ".py",
    "PyPy 3": ".py", "PyPy 3-64": ".py",
    "Ruby 3": ".rb", "Ruby": ".rb",
    "Rust": ".rs", "Rust 2021": ".rs",
    "Scala": ".scala", "JavaScript": ".js", "Node.js": ".js", "TypeScript": ".ts",
    "Tcl": ".tcl", "Io": ".io", "Pike": ".pike",
    "Befunge": ".bf", "Cobol": ".cob", "Factor": ".factor",
    "Secret_171": ".txt", "Roco": ".txt", "Ada": ".adb",
    "Mysterious Language": ".txt", "FALSE": ".f", "Picat": ".pi",
    "Clojure": ".clj", "Julia": ".jl",
}


def get_extension(language: str) -> str:
    if language in LANG_EXT_MAP:
        return LANG_EXT_MAP[language]
    lang_lower = language.lower()
    for key, ext in LANG_EXT_MAP.items():
        key_lower = key.lower()
        if len(key_lower) >= 3 and (key_lower in lang_lower or lang_lower in key_lower):
            return ext
    return ".txt"


def sanitize(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*.]', "_", name)
    name = re.sub(r"[\s_]+", "_", name)
    name = name.strip("_. ")
    return name if name else "Unknown"


# -- CF API --

def cf_api_request(method: str, params: Optional[Dict] = None) -> dict:
    if params is None:
        params = {}
    rand_prefix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    current_time = str(int(time.time()))
    params["apiKey"] = CF_API_KEY
    params["time"] = current_time
    sorted_params = sorted(params.items())
    param_string = "&".join(f"{k}={v}" for k, v in sorted_params)
    sig_input = f"{rand_prefix}/{method}?{param_string}#{CF_API_SECRET}"
    sig_hash = hashlib.sha512(sig_input.encode("utf-8")).hexdigest()
    api_sig = rand_prefix + sig_hash
    params["apiSig"] = api_sig
    url = f"{CF_API_BASE}/{method}"
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=60)
            break
        except requests.exceptions.Timeout:
            if attempt < 2:
                time.sleep(5)
            else:
                raise
    if resp.status_code != 200:
        print(f"    API {method} -> {resp.status_code}")
        try:
            print(f"    {resp.json()}")
        except Exception:
            print(f"    {resp.text[:300]}")
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "OK":
        raise RuntimeError(f"CF API error: {data.get('comment', 'Unknown')}")
    return data["result"]


def verify_api_auth():
    print("  Verifying CF API auth...")
    try:
        cf_api_request("user.friends", {"onlyOnline": "false"})
        print("  API key valid!")
        return True
    except Exception as e:
        print(f"  ERROR: {e}")
        print(f"  Key: {CF_API_KEY[:8]}... Secret set: {'yes' if CF_API_SECRET else 'NO'}")
        return False


def fetch_accepted_submissions() -> List[dict]:
    all_subs = []
    page = 1
    while True:
        print(f"  Fetching page {page}...")
        result = cf_api_request("user.status", {
            "handle": CF_HANDLE,
            "from": str((page - 1) * 1000 + 1),
            "count": "1000",
            "includeSources": "true",
        })
        if not result:
            break
        all_subs.extend(result)
        if len(result) < 1000:
            break
        page += 1
        time.sleep(2)
    accepted = [s for s in all_subs if s.get("verdict") == "OK"]
    print(f"  {len(accepted)} accepted / {len(all_subs)} total")
    return accepted


def fetch_contest_names() -> Dict[int, str]:
    print("  Fetching contest names...")
    mapping = {}
    for gym in ["false", "true"]:
        try:
            contests = cf_api_request("contest.list", {"gym": gym})
            for c in contests:
                mapping[c["id"]] = c.get("name", f"Contest_{c['id']}")
        except Exception as e:
            print(f"    Warning: contest.list gym={gym} failed: {e}")
        time.sleep(2)
    return mapping


def resolve_missing_names(subs: List[dict], mapping: Dict[int, str]) -> Dict[int, str]:
    missing = set()
    for sub in subs:
        cid = sub.get("contestId") or sub.get("problem", {}).get("contestId")
        if cid and cid not in mapping:
            missing.add(cid)
    for cid in sorted(missing):
        try:
            result = cf_api_request("contest.status", {"contestId": str(cid)})
            contest_info = result.get("contest", {})
            name = contest_info.get("name", f"Contest_{cid}")
            mapping[cid] = name
            print(f"    Resolved {cid}: {name}")
        except Exception:
            mapping[cid] = f"Contest_{cid}"
        time.sleep(2)
    return mapping


# -- GitHub --

def load_pushed_state(repo) -> Set[int]:
    try:
        contents = repo.get_contents(STATE_FILE)
        data = json.loads(contents.decoded_content.decode("utf-8"))
        return set(data.get("pushed_ids", []))
    except GithubException:
        return set()


def save_pushed_state(repo, pushed_ids: Set[int], branch: str):
    content = json.dumps({"pushed_ids": sorted(pushed_ids)}, indent=2)
    try:
        existing = repo.get_contents(STATE_FILE, ref=branch)
        repo.update_file(STATE_FILE, "Update state", content, existing.sha, branch=branch)
    except GithubException:
        repo.create_file(STATE_FILE, "Init state", content, branch=branch)


def batch_commit_files(repo, file_dict: Dict[str, str], message: str, branch: str):
    if not file_dict:
        return
    for attempt in range(3):
        try:
            ref = repo.get_git_ref(f"heads/{branch}")
            latest_commit = repo.get_git_commit(ref.object.sha)
            base_tree = latest_commit.tree
            tree_elements = []
            for path, content in file_dict.items():
                blob = repo.create_git_blob(content, "utf-8")
                tree_elements.append(InputGitTreeElement(path=path, mode="100644", type="blob", sha=blob.sha))
            new_tree = repo.create_git_tree(tree_elements, base_tree)
            new_commit = repo.create_git_commit(message, new_tree, [latest_commit])
            ref.edit(new_commit.sha)
            return
        except GithubException as e:
            if "not a fast forward" in str(e) and attempt < 2:
                time.sleep(5)
                continue
            raise


# -- Dedup --

def deduplicate_submissions(subs: List[dict]) -> List[dict]:
    best = {}
    for sub in subs:
        problem = sub.get("problem", {})
        cid = sub.get("contestId") or problem.get("contestId")
        idx = problem.get("index", "")
        key = (cid, idx)
        if key not in best or sub["id"] > best[key]["id"]:
            best[key] = sub
    return list(best.values())


# -- File path --

def build_file_path(sub: dict, contest_names: Dict[int, str]) -> str:
    problem = sub.get("problem", {})
    contest_id = sub.get("contestId") or problem.get("contestId")
    index = problem.get("index", "X")
    language = sub.get("programmingLanguage", "Unknown")
    ext = get_extension(language)
    rating = problem.get("rating")
    problem_name = problem.get("name", "")
    safe_name = sanitize(problem_name) if problem_name else ""

    if rating is not None:
        filename = f"{contest_id}{index}_{safe_name}{ext}" if safe_name else f"{contest_id}{index}{ext}"
        path = f"{rating}/{filename}"
    else:
        contest_name = contest_names.get(contest_id)
        if contest_name and not contest_name.startswith(f"Contest_{contest_id}"):
            folder = sanitize(f"{contest_name}_{contest_id}")
        else:
            folder = f"Contest_{contest_id}"
        filename = f"{index}_{safe_name}{ext}" if safe_name else f"{index}{ext}"
        path = f"Contests/{folder}/{filename}"

    if TARGET_DIR:
        path = f"{TARGET_DIR}/{path}"
    return path


# -- Main --

def validate_config():
    missing = []
    for var in ["CF_API_KEY", "CF_API_SECRET", "CF_HANDLE", "GH_PAT", "TARGET_REPO"]:
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        print(f"Error: Missing env vars: {', '.join(missing)}")
        sys.exit(1)


def main():
    validate_config()
    print(f"=== Codeforces AutoPush ===")
    print(f"Handle: {CF_HANDLE}")
    print(f"Target: {TARGET_REPO}")
    print(f"Dir: {TARGET_DIR or '(root)'}")
    print()

    print("[1/6] GitHub...")
    gh = Github(auth=Auth.Token(GH_PAT))
    repo = gh.get_repo(TARGET_REPO)
    branch = repo.default_branch
    print(f"  OK. Branch: {branch}")

    print("[2/6] CF auth...")
    if not verify_api_auth():
        sys.exit(1)

    print("[3/6] State...")
    pushed_ids = load_pushed_state(repo)
    print(f"  Already: {len(pushed_ids)}")

    print("[4/6] Fetching...")
    accepted = fetch_accepted_submissions()
    if not accepted:
        print("  None found.")
        return
    unique = deduplicate_submissions(accepted)
    print(f"  Unique: {len(unique)}")
    new_subs = [s for s in unique if s["id"] not in pushed_ids]
    print(f"  New: {len(new_subs)}")
    if not new_subs:
        print("  Up to date.")
        return

    print("[5/6] Contest names...")
    contest_names = fetch_contest_names()
    contest_names = resolve_missing_names(new_subs, contest_names)

    print(f"[6/6] Pushing {len(new_subs)} files...")
    file_map = {}
    newly = set()
    skipped = 0
    for sub in new_subs:
        b64 = sub.get("sourceBase64", "")
        source = None
        if b64:
            try:
                source = base64.b64decode(b64).decode("utf-8", errors="replace")
            except Exception:
                pass
        if source:
            file_map[build_file_path(sub, contest_names)] = source
            newly.add(sub["id"])
        else:
            p = sub.get("problem", {})
            print(f"    Skip {p.get('index','?')} (no source)")
            skipped += 1

    print(f"  Built: {len(file_map)}, Skipped: {skipped}")
    if not file_map:
        print("  ERROR: No source fetched.")
        sys.exit(1)

    paths = list(file_map.keys())
    for i in range(0, len(paths), BATCH_SIZE):
        batch = {p: file_map[p] for p in paths[i:i+BATCH_SIZE]}
        n = (i // BATCH_SIZE) + 1
        t = (len(paths) + BATCH_SIZE - 1) // BATCH_SIZE
        msg = f"Add {len(newly)} solutions" if t == 1 else f"Solutions {n}/{t}"
        print(f"  Batch {n}/{t} ({len(batch)} files)")
        batch_commit_files(repo, batch, msg, branch)

    pushed_ids.update(newly)
    save_pushed_state(repo, pushed_ids, branch)
    print(f"\nDone! Pushed {len(newly)}. Total: {len(pushed_ids)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nFATAL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)   
