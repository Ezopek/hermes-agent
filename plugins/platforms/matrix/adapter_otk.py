# Copyright (c) 2023 Tulir Asokan
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Durable OTK publication, adapted from mautrix 0.21.1 OlmMachine._share_keys.

Commit-before-upload follows the MIT-licensed contribution
in https://github.com/NousResearch/hermes-agent/pull/101220, pinned at
https://github.com/NousResearch/hermes-agent/commit/7894ad17615c1a49dd684a8a7fea1232a75abe47
(MatrixKeyRecoveryMixin._share_matrix_keys_crash_safely).
Copyright (c) 2025 Nous Research; MIT permission/disclaimer in repository LICENSE.

Unlike that contribution, a batch found unpublished is never uploaded again: its upload
may have been accepted, so a peer may already have claimed a key, and publishing the same
ID again lets a second peer claim it too.
"""

import time
from collections.abc import Mapping
from typing import Any


def _validate_key_counts(counts: Any) -> None:
    if not isinstance(counts, Mapping) or any(
        isinstance(count, bool) or not isinstance(count, int) or count < 0
        for count in counts.values()
    ):
        raise ValueError("Invalid one-time key counts response")


async def share_keys_crash_safely(olm: Any, current_otk_count: int | None) -> None:
    """Called inside the SDK's public share_keys lock, including startup and sync."""
    if current_otk_count is None or (
        olm._last_key_share + 60 > time.monotonic() and current_otk_count < 10
    ):
        olm.log.debug("Checking OTK count on server")
        counts = await olm.client.upload_keys()
        _validate_key_counts(counts)
        current_otk_count = next(
            (count for algorithm, count in counts.items() if str(algorithm) == "signed_curve25519"),
            0,
        )
    device_keys = (
        olm.account.get_device_keys(olm.client.mxid, olm.client.device_id)
        if not olm.account.shared
        else None
    )
    # Keys still unpublished here come from an upload whose outcome is unknown: it may
    # have been accepted, and the sync that reports the claim-lowered count runs this
    # before the claimant's first message is decrypted. Retire them: the private keys
    # stay for sessions peers already started, but these IDs are never published again.
    olm.account.mark_keys_as_published()
    one_time_keys = olm.account.get_one_time_keys(
        olm.client.mxid, olm.client.device_id, current_otk_count
    )
    if not device_keys and not one_time_keys:
        olm.log.warning("No one-time keys nor device keys got when trying to share keys")
        return
    if device_keys:
        olm.log.debug("Going to upload initial account keys")
    olm.log.debug("Uploading %d one-time keys", len(one_time_keys))
    if one_time_keys:
        # Public keys must not escape until their private halves are committed.
        await olm.crypto_store.put_account(olm.account)
    response = await olm.client.upload_keys(one_time_keys=one_time_keys, device_keys=device_keys)
    _validate_key_counts(response)
    olm.account.shared = True
    olm.account.mark_keys_as_published()
    olm._last_key_share = time.monotonic()
    # If this save fails, propagate once. The pre-upload pickle still contains
    # the private keys for a reload; publication never deletes them.
    await olm.crypto_store.put_account(olm.account)
    olm.log.debug("Shared keys and saved account, new keys: %s", response)
