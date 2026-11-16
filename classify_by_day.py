#!/usr/bin/env python3
"""Mirror the latest accepted Codeforces solutions into date folders in the target repo."""

import os
import posixpath
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from github import Auth, Github, GithubException

import cf_autopush as autopush

LOCAL_TZ = ZoneInfo("Asia/Kolkata")


def main():
    autopush.validate_config()
    print(f"Fetching accepted submissions for {autopush.CF_HANDLE}...")
    if not autopush.verify_api_auth():
        raise RuntimeError("Codeforces API authentication failed.")

    accepted = autopush.fetch_accepted_submissions()
    latest = autopush.deduplicate_submissions(accepted)
    print(f"Latest accepted problems: {len(latest)}")

    contest_names = autopush.fetch_contest_names()
    contest_names = autopush.resolve_missing_names(latest, contest_names)

    github = Github(auth=Auth.Token(autopush.GH_PAT))
    repo = github.get_repo(autopush.TARGET_REPO)
    branch = repo.default_branch
    files = {}
    skipped = 0

    for submission in latest:
        source_b64 = submission.get("sourceBase64", "")
        if not source_b64:
            skipped += 1
            continue
        try:
            source = autopush.base64.b64decode(source_b64).decode("utf-8", errors="replace")
        except Exception:
            skipped += 1
            continue

        original_path = autopush.build_file_path(submission, contest_names)
        filename = posixpath.basename(original_path)
        submitted = datetime.fromtimestamp(
            submission["creationTimeSeconds"], tz=timezone.utc
        ).astimezone(LOCAL_TZ)
        date_dir = f"{submitted.year % 100:02d}/{submitted.month}/{submitted.day}"
        date_path = f"{autopush.TARGET_DIR}/{date_dir}/{filename}" if autopush.TARGET_DIR else f"{date_dir}/{filename}"

        try:
            existing = repo.get_contents(date_path, ref=branch)
            if existing.decoded_content.decode("utf-8", errors="replace") == source:
                continue
        except GithubException as exc:
            if exc.status != 404:
                raise

        files[date_path] = source

    if not files:
        print(f"Date folders are up to date. Sources unavailable: {skipped}.")
        return

    paths = list(files)
    for offset in range(0, len(paths), autopush.BATCH_SIZE):
        batch = {path: files[path] for path in paths[offset:offset + autopush.BATCH_SIZE]}
        message = f"Add {len(batch)} Codeforces date-folder solution(s)"
        autopush.batch_commit_files(repo, batch, message, branch)

    print(f"Added or refreshed {len(files)} date-folder copies. Sources unavailable: {skipped}.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
