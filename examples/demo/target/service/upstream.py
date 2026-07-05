"""The file examples/demo/work_unit.diff represents, in its post-diff state.

This is the demo's auto-apply TARGET: a real, on-disk file carrying the two bugs
the demo diff introduces -- a secret logged in cleartext and an outbound HTTP call
with no timeout. The offline demo (run_demo.py) and the end-to-end test
(tests/test_autoapply_e2e.py) COPY this file into a scratch directory, let LAZARUS
auto-apply the stub judge's concrete edits against the copy, and then `lazarus
undo` to restore it -- so this committed original is never mutated.

The stub judge (examples/demo/stub_judge.py) targets two lines below by exact
substring: the api-key logging call in fetch_profile, and the profile-service GET
call. Those two lines must stay byte-identical to the stub's `find` values, or the
demo's auto-apply step will skip (no unique match) instead of applying. Do NOT
quote either line verbatim anywhere else in this file -- a second occurrence makes
the match ambiguous and the conservative applier skips it.
"""

import logging

import requests

logger = logging.getLogger(__name__)


def fetch_profile(user_id, api_key):
    # Build the request URL and headers for the upstream profile service.
    url = f"https://api.example.com/v1/users/{user_id}/profile"
    headers = {"authorization": f"Bearer {api_key}"}

    # Log what we are about to do so the call is traceable in production.
    logger.info(f"fetching profile for user {user_id} with api key {api_key}")

    # Call the external profile service and return the parsed JSON body.
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()

    logger.info(f"fetched profile for user {user_id}: {len(data)} fields")
    return data
