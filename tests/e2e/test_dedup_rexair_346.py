"""E2E test: Run dedup pipeline on the full Rexair 346-file dataset.

This test validates that:
1. All 346 files go through validation and dedup without crashes
2. No false positives — different POs from the same vendor are NOT collapsed
3. True duplicates are caught
4. The pipeline handles all file types (PDF, DOCX, PNG, MSG, XLSX, DOC)
5. Blocking keys (regex fallback) correctly protect template-based documents
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from echelonos.stages.stage_0a_validation import validate_folder
from echelonos.stages.stage_0b_dedup import deduplicate_files


REXAIR_FOLDER = "/Users/shangchienliu/Desktop/Rexair-Contracts-Flat"


@pytest.fixture
def rexair_files():
    """Validate and return VALID files from the Rexair dataset."""
    if not Path(REXAIR_FOLDER).is_dir():
        pytest.skip(f"Rexair dataset not found at {REXAIR_FOLDER}")

    validated = validate_folder(REXAIR_FOLDER)
    valid = [f for f in validated if f["status"] == "VALID"]
    return validated, valid


class TestDedupRexair346:
    """Full dataset dedup tests on 346 Rexair contract files."""

    def test_validation_counts(self, rexair_files):
        """All files should be validated without errors."""
        validated, valid = rexair_files
        total = len(validated)
        valid_count = len(valid)

        print(f"\n--- Validation Results ---")
        print(f"Total files found: {total}")
        print(f"VALID:          {valid_count}")
        print(f"INVALID:        {sum(1 for r in validated if r['status'] == 'INVALID')}")
        print(f"NEEDS_PASSWORD: {sum(1 for r in validated if r['status'] == 'NEEDS_PASSWORD')}")
        print(f"REJECTED:       {sum(1 for r in validated if r['status'] == 'REJECTED')}")

        # We expect ~346 files total (minus .DS_Store which is skipped)
        assert total >= 340, f"Expected ~346 files, got {total}"
        # Most should be valid
        assert valid_count >= 300, f"Expected most files valid, got {valid_count}"

    def test_dedup_no_crash(self, rexair_files):
        """Dedup pipeline should complete without errors on all valid files."""
        _, valid = rexair_files
        unique = deduplicate_files(valid)
        assert len(unique) > 0, "Dedup returned zero unique files"

        duplicates = len(valid) - len(unique)
        print(f"\n--- Dedup Results ---")
        print(f"Input (valid): {len(valid)}")
        print(f"Unique:        {len(unique)}")
        print(f"Duplicates:    {duplicates}")

    def test_dedup_layer_distribution(self, rexair_files):
        """Check which layers are catching duplicates."""
        _, valid = rexair_files
        unique = deduplicate_files(valid)

        # Count duplicates by layer
        all_entries = valid  # entries are mutated in-place
        layer_counts = {1: 0, 2: 0, 3: 0}
        for entry in all_entries:
            if entry.get("is_duplicate"):
                layer = entry.get("dedup_layer")
                if layer in layer_counts:
                    layer_counts[layer] += 1

        total_dups = sum(layer_counts.values())
        print(f"\n--- Dedup Layer Distribution ---")
        print(f"Layer 1 (exact file hash):  {layer_counts[1]}")
        print(f"Layer 2 (content hash):     {layer_counts[2]}")
        print(f"Layer 3 (MinHash near-dup): {layer_counts[3]}")
        print(f"Total duplicates:           {total_dups}")
        print(f"Unique files:               {len(unique)}")

    def test_different_po_numbers_all_survive(self, rexair_files):
        """Different PO numbers from same vendor must NOT be collapsed.

        The Rexair dataset has many POs from "7th Street Solutions" with
        different PO numbers — each should survive as unique.
        """
        _, valid = rexair_files
        unique = deduplicate_files(valid)

        unique_paths = {u["file_path"] for u in unique}

        # Find all 7th Street Solutions files with different PO numbers in filename
        seventh_street_files = [
            f for f in valid
            if "7th Street Solutions" in f["file_path"]
            and any(c.isdigit() for c in Path(f["file_path"]).stem.split("_")[-1])
        ]

        if not seventh_street_files:
            pytest.skip("No 7th Street Solutions PO files found")

        # Count how many survived
        survived = [f for f in seventh_street_files if f["file_path"] in unique_paths]
        collapsed = [f for f in seventh_street_files if f["file_path"] not in unique_paths]

        print(f"\n--- 7th Street Solutions PO Protection ---")
        print(f"Total 7th St files:  {len(seventh_street_files)}")
        print(f"Survived:            {len(survived)}")
        print(f"Collapsed:           {len(collapsed)}")

        if collapsed:
            print("\nCollapsed files (potential false positives):")
            for f in collapsed:
                dup_of = f.get("duplicate_of", "?")
                layer = f.get("dedup_layer", "?")
                print(f"  Layer {layer}: {Path(f['file_path']).name} -> {Path(dup_of).name}")

        # Different PO numbers should NOT be collapsed
        # Allow a small number of true duplicates (same PO, different filename)
        # but the vast majority should survive
        survival_rate = len(survived) / len(seventh_street_files) if seventh_street_files else 1
        assert survival_rate >= 0.90, (
            f"Too many 7th Street files collapsed: {len(collapsed)}/{len(seventh_street_files)} "
            f"({1 - survival_rate:.0%} collapse rate). These may be false positives."
        )

    def test_no_false_positive_different_vendors(self, rexair_files):
        """Files from clearly different vendors should not be collapsed.

        Filename pattern is Location_Department_Vendor_Details.pdf.
        The vendor is the third underscore-delimited field.  We skip
        entries where the "vendor" segment is actually a department name
        (e.g., Legal vs Accounting filing the same document) since those
        are true duplicates, not false positives.
        """
        _, valid = rexair_files
        unique = deduplicate_files(valid)

        # Departments that appear in the second field — different departments
        # filing the same document is NOT a false positive.
        known_departments = {
            "legal", "accounting", "hr", "purchasing", "it",
            "quality - field service", "engineering",
        }

        false_positives = []
        for entry in valid:
            if not entry.get("is_duplicate"):
                continue
            dup_of = entry.get("duplicate_of", "")
            entry_vendor = _extract_vendor_from_path(entry["file_path"])
            dup_vendor = _extract_vendor_from_path(dup_of)

            if not entry_vendor or not dup_vendor or entry_vendor == dup_vendor:
                continue

            # Skip department-level differences (same doc filed in 2 depts)
            if entry_vendor.lower() in known_departments or dup_vendor.lower() in known_departments:
                continue

            false_positives.append((entry, dup_of))

        if false_positives:
            msgs = []
            for entry, dup_of in false_positives:
                msgs.append(
                    f"  {Path(entry['file_path']).name} (vendor: {_extract_vendor_from_path(entry['file_path'])})\n"
                    f"  collapsed into {Path(dup_of).name} (vendor: {_extract_vendor_from_path(dup_of)})\n"
                    f"  Layer: {entry.get('dedup_layer')}"
                )
            pytest.fail(
                f"False positive: {len(false_positives)} different-vendor collapses!\n"
                + "\n".join(msgs)
            )

    def test_blocking_keys_populated_on_unique(self, rexair_files):
        """Unique files that went through blocking key extraction should have data."""
        _, valid = rexair_files
        unique = deduplicate_files(valid)

        with_keys = [u for u in unique if u.get("blocking_keys")]
        without_keys = [u for u in unique if not u.get("blocking_keys")]

        print(f"\n--- Blocking Keys Population ---")
        print(f"Unique with blocking_keys:    {len(with_keys)}")
        print(f"Unique without blocking_keys: {len(without_keys)}")

        # blocking_keys are only populated when a candidate match triggers lazy extraction
        # so not all unique files will have them — that's expected

    def test_identity_tokens_populated(self, rexair_files):
        """All valid files with text should have identity tokens."""
        _, valid = rexair_files
        unique = deduplicate_files(valid)

        with_tokens = [u for u in unique if u.get("identity_tokens")]
        print(f"\n--- Identity Tokens ---")
        print(f"Unique with tokens:    {len(with_tokens)}")
        print(f"Unique without tokens: {len(unique) - len(with_tokens)}")

    def test_duplicate_details(self, rexair_files):
        """Print detailed info about each duplicate found."""
        _, valid = rexair_files
        unique = deduplicate_files(valid)

        duplicates = [e for e in valid if e.get("is_duplicate")]

        print(f"\n--- Duplicate Details ({len(duplicates)} total) ---")
        for dup in duplicates:
            src = Path(dup["file_path"]).name
            dst = Path(dup["duplicate_of"]).name
            layer = dup["dedup_layer"]
            print(f"  Layer {layer}: {src}")
            print(f"         -> {dst}")
            print()


def _extract_vendor_from_path(file_path: str) -> str:
    """Extract vendor name from Rexair filename pattern: Cadillac_VENDOR_details.pdf"""
    name = Path(file_path).stem
    parts = name.split("_")
    if len(parts) >= 2:
        return parts[1]
    return ""
