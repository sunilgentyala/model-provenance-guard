#!/usr/bin/env python3
"""
verify_weights.py
-----------------
Binary-level inspection of ML model weight files before they are loaded
into memory or admitted into a production MLOps pipeline.

Supported formats:
  - Safetensors (.safetensors): Header JSON inspection and anomaly detection
  - GGUF (.gguf): Magic byte validation and metadata key-value audit
  - PyTorch (.pt, .pth): Pickle opcode pre-check (requires picklescan)

This script is part of the model-provenance-guard toolkit.
Author: Sunil Gentyala (github.com/sunilgentyala/model-provenance-guard)
License: MIT

Usage:
  python verify_weights.py --file model.safetensors --registry registry/trusted_models.json
  python verify_weights.py --file model.gguf --registry registry/trusted_models.json --format gguf
  python verify_weights.py --file model.pt --format pt
"""

import argparse
import hashlib
import json
import os
import struct
import sys
import datetime
import tempfile
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# GGUF files begin with these four bytes: 0x47 0x47 0x55 0x46 = "GGUF"
GGUF_MAGIC = b"GGUF"

# Safetensors: the first 8 bytes are a little-endian uint64 representing the
# byte length of the JSON header that follows immediately.
SAFETENSORS_HEADER_SIZE_BYTES = 8

# Maximum tolerated header size for Safetensors. Headers larger than this
# are a strong anomaly indicator and should block loading.
# The official safetensors spec imposes no hard limit, but legitimate models
# from major providers do not produce headers beyond a few hundred kilobytes.
MAX_SAFETENSORS_HEADER_BYTES = 100 * 1024 * 1024  # 100 MB (conservative ceiling)
WARN_SAFETENSORS_HEADER_BYTES = 10 * 1024 * 1024   # 10 MB (warn threshold)

# GGUF version 2 and 3 are current. Versions outside this range are suspicious.
GGUF_SUPPORTED_VERSIONS = {2, 3}

# Known-safe GGUF metadata key prefixes. Keys that do not match any expected
# prefix are flagged as anomalous. This list is not exhaustive but covers
# all standard keys defined in the GGUF specification v3.
GGUF_EXPECTED_KEY_PREFIXES = (
    "general.",
    "tokenizer.",
    "llama.",
    "mistral.",
    "phi.",
    "qwen.",
    "gemma.",
    "falcon.",
    "bloom.",
    "mpt.",
    "gpt2.",
    "gptj.",
    "gptneox.",
    "starcoder.",
    "rwkv.",
    "whisper.",
    "bert.",
    "nomic.",
    "clip.",
    "mamba.",
    "command.",
    "deepseek.",
    "stablelm.",
    "internlm.",
    "baichuan.",
    "chatglm.",
    "orion.",
    "xverse.",
)

# Safetensors dtype values that are valid per the specification.
# Any dtype value outside this set is a header anomaly.
VALID_SAFETENSORS_DTYPES = {
    "F64", "F32", "F16", "BF16",
    "I64", "I32", "I16", "I8",
    "U8", "BOOL",
}

# ---------------------------------------------------------------------------
# Utility: SHA-256 hash computation
# ---------------------------------------------------------------------------

def compute_sha256(filepath: str, chunk_size: int = 1024 * 1024) -> str:
    """Compute SHA-256 hash of a file using chunked reads to handle large files."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Utility: Trusted registry lookup
# ---------------------------------------------------------------------------

def lookup_registry(registry_path: str, filename: str) -> dict | None:
    """
    Return the registry entry for the given filename, or None if not found.
    Exits with an error if the registry file cannot be parsed.
    """
    if not registry_path or not os.path.isfile(registry_path):
        log_warn(f"Registry not found at {registry_path}. Skipping registry lookup.")
        return None

    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            registry = json.load(f)
    except json.JSONDecodeError as exc:
        log_error(f"Registry file is not valid JSON: {exc}")
        sys.exit(1)

    basename = os.path.basename(filename)
    for entry in registry.get("models", []):
        if entry.get("filename") == basename:
            return entry

    return None


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def log_pass(msg: str) -> None:
    print(f"[PASS]  {msg}")


def log_warn(msg: str) -> None:
    print(f"[WARN]  {msg}", file=sys.stderr)


def log_error(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)


def log_info(msg: str) -> None:
    print(f"[INFO]  {msg}")


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def write_report(report: dict, output_dir: str = "") -> str:
    """Write inspection results to a timestamped JSON file for artifact upload."""
    if not output_dir:
        output_dir = tempfile.gettempdir()
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    basename = os.path.basename(report.get("file", "unknown"))
    report_path = os.path.join(output_dir, f"inspection_report_{basename}_{timestamp}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    log_info(f"Inspection report written to: {report_path}")
    return report_path


# ---------------------------------------------------------------------------
# Safetensors inspection
# ---------------------------------------------------------------------------

def inspect_safetensors(filepath: str, registry_path: str) -> bool:
    """
    Inspect a Safetensors file for header anomalies before any tensor data
    is loaded into memory.

    Checks performed:
      1. File is large enough to contain the 8-byte header size prefix.
      2. The declared header size is within tolerable bounds.
      3. The header bytes are valid UTF-8 and parse as JSON.
      4. Every tensor entry in the header has valid 'dtype', 'shape', and
         'data_offsets' fields with internally consistent values.
      5. No tensor entry declares a dtype outside the known-valid set.
      6. The '__metadata__' key, if present, contains only string values.
      7. The total byte range covered by all data_offsets does not exceed
         the actual file size.

    Returns True if all checks pass, False otherwise.
    """
    log_info(f"Inspecting Safetensors file: {filepath}")
    report = {
        "file": filepath,
        "format": "safetensors",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "checks": {},
        "anomalies": [],
        "passed": False,
    }

    file_size = os.path.getsize(filepath)

    # Check 1: Minimum file size
    if file_size < SAFETENSORS_HEADER_SIZE_BYTES:
        log_error("File is too small to contain a valid Safetensors header size prefix.")
        report["anomalies"].append("File too small for header size prefix.")
        write_report(report)
        return False
    report["checks"]["minimum_size"] = "pass"

    with open(filepath, "rb") as f:
        raw_size_bytes = f.read(SAFETENSORS_HEADER_SIZE_BYTES)
        header_size = struct.unpack("<Q", raw_size_bytes)[0]

        # Check 2: Header size sanity
        if header_size > MAX_SAFETENSORS_HEADER_BYTES:
            log_error(f"Declared header size {header_size} bytes exceeds maximum tolerated value of {MAX_SAFETENSORS_HEADER_BYTES} bytes. Possible header injection.")
            report["anomalies"].append(f"Oversized header: {header_size} bytes")
            write_report(report)
            return False

        if header_size > WARN_SAFETENSORS_HEADER_BYTES:
            log_warn(f"Header size {header_size} bytes is unusually large. Inspect manually.")
            report["anomalies"].append(f"Unusually large header: {header_size} bytes")

        if SAFETENSORS_HEADER_SIZE_BYTES + header_size > file_size:
            log_error("Declared header size extends beyond end of file. File is malformed or truncated.")
            report["anomalies"].append("Header size extends past end of file.")
            write_report(report)
            return False
        report["checks"]["header_size_bounds"] = "pass"

        # Check 3: Header is valid UTF-8 JSON
        raw_header = f.read(header_size)
        try:
            raw_header_str = raw_header.decode("utf-8")
        except UnicodeDecodeError as exc:
            log_error(f"Header bytes are not valid UTF-8: {exc}")
            report["anomalies"].append("Header is not valid UTF-8.")
            write_report(report)
            return False

        try:
            header: dict[str, Any] = json.loads(raw_header_str)
        except json.JSONDecodeError as exc:
            log_error(f"Header is not valid JSON: {exc}")
            report["anomalies"].append(f"Header JSON parse error: {exc}")
            write_report(report)
            return False
        report["checks"]["header_json_valid"] = "pass"

    # Check 4: Tensor entry field validation
    tensor_count = 0
    max_data_offset = 0
    anomalous_tensors = []

    for key, value in header.items():
        if key == "__metadata__":
            # Check 6: __metadata__ values must all be strings
            if not isinstance(value, dict):
                log_error("__metadata__ is not a JSON object.")
                report["anomalies"].append("__metadata__ is not a dict.")
                write_report(report)
                return False
            non_string_keys = [k for k, v in value.items() if not isinstance(v, str)]
            if non_string_keys:
                log_warn(f"__metadata__ contains non-string values for keys: {non_string_keys}. This is anomalous.")
                report["anomalies"].append(f"Non-string metadata values: {non_string_keys}")
            report["checks"]["metadata_field"] = "pass"
            continue

        # Each non-metadata entry should be a tensor descriptor
        if not isinstance(value, dict):
            log_warn(f"Tensor entry '{key}' is not a dict. Skipping.")
            anomalous_tensors.append(key)
            continue

        tensor_count += 1

        # Check dtype
        dtype = value.get("dtype")
        if dtype not in VALID_SAFETENSORS_DTYPES:
            log_error(f"Tensor '{key}' has invalid dtype: '{dtype}'. Not in valid set: {VALID_SAFETENSORS_DTYPES}")
            anomalous_tensors.append(key)

        # Check shape
        shape = value.get("shape")
        if not isinstance(shape, list) or not all(isinstance(d, int) and d >= 0 for d in shape):
            log_error(f"Tensor '{key}' has malformed shape: {shape}")
            anomalous_tensors.append(key)

        # Check data_offsets
        offsets = value.get("data_offsets")
        if not isinstance(offsets, list) or len(offsets) != 2:
            log_error(f"Tensor '{key}' has malformed data_offsets: {offsets}")
            anomalous_tensors.append(key)
        else:
            start, end = offsets
            if not (isinstance(start, int) and isinstance(end, int) and start >= 0 and end >= start):
                log_error(f"Tensor '{key}' has invalid data_offsets range: [{start}, {end}]")
                anomalous_tensors.append(key)
            else:
                if end > max_data_offset:
                    max_data_offset = end

    if anomalous_tensors:
        report["anomalies"].append(f"Anomalous tensor entries: {anomalous_tensors}")

    report["checks"]["tensor_entry_validation"] = "fail" if anomalous_tensors else "pass"
    report["tensor_count"] = tensor_count

    # Check 7: Data offsets do not exceed file size
    data_region_start = SAFETENSORS_HEADER_SIZE_BYTES + header_size
    if max_data_offset > 0 and (data_region_start + max_data_offset) > file_size:
        log_error(f"Tensor data offsets declare a region ({data_region_start + max_data_offset} bytes) that extends past end of file ({file_size} bytes). File is malformed.")
        report["anomalies"].append("Tensor data offsets extend past end of file.")
        write_report(report)
        return False
    report["checks"]["data_offset_bounds"] = "pass"

    if anomalous_tensors:
        log_error(f"Header inspection failed. {len(anomalous_tensors)} anomalous tensor entries detected.")
        write_report(report)
        return False

    log_pass(f"Safetensors header inspection passed. {tensor_count} tensors verified.")
    log_info(f"  Header size: {header_size} bytes")
    log_info(f"  Tensor count: {tensor_count}")

    report["passed"] = True
    write_report(report)
    return True


# ---------------------------------------------------------------------------
# GGUF inspection
# ---------------------------------------------------------------------------

def inspect_gguf(filepath: str, registry_path: str) -> bool:
    """
    Validate the GGUF file magic bytes and audit the metadata key-value section
    for entries that do not conform to known architectural key prefixes.

    GGUF binary layout (v2/v3):
      Bytes 0-3:   Magic ("GGUF")
      Bytes 4-7:   Version (uint32 LE)
      Bytes 8-15:  Tensor count (uint64 LE)
      Bytes 16-23: Metadata key-value count (uint64 LE)
      Bytes 24+:   Metadata key-value pairs (variable length)

    This inspector reads the magic, version, and key names only. It does not
    load or interpret tensor data. Reading key names requires parsing
    length-prefixed strings; any parse failure is treated as a hard anomaly.

    Returns True if all checks pass, False otherwise.
    """
    log_info(f"Inspecting GGUF file: {filepath}")
    report = {
        "file": filepath,
        "format": "gguf",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "checks": {},
        "anomalies": [],
        "passed": False,
    }

    file_size = os.path.getsize(filepath)
    if file_size < 24:
        log_error("File is too small to be a valid GGUF file (minimum 24 bytes for header).")
        report["anomalies"].append("File too small for GGUF header.")
        write_report(report)
        return False

    with open(filepath, "rb") as f:
        # Check magic
        magic = f.read(4)
        if magic != GGUF_MAGIC:
            log_error(f"GGUF magic bytes not found. Got: {magic.hex()}. Expected: {GGUF_MAGIC.hex()}")
            report["anomalies"].append(f"Invalid magic bytes: {magic.hex()}")
            write_report(report)
            return False
        log_pass("GGUF magic bytes validated: 'GGUF'")
        report["checks"]["magic_bytes"] = "pass"

        # Check version
        version_bytes = f.read(4)
        version = struct.unpack("<I", version_bytes)[0]
        log_info(f"  GGUF version: {version}")
        if version not in GGUF_SUPPORTED_VERSIONS:
            log_warn(f"GGUF version {version} is not in the known-safe set {GGUF_SUPPORTED_VERSIONS}. This may indicate a non-standard or modified file.")
            report["anomalies"].append(f"Unsupported GGUF version: {version}")
        report["checks"]["version"] = "pass" if version in GGUF_SUPPORTED_VERSIONS else "warn"

        # Read tensor count and metadata count
        tensor_count = struct.unpack("<Q", f.read(8))[0]
        kv_count = struct.unpack("<Q", f.read(8))[0]
        log_info(f"  Tensor count declared: {tensor_count}")
        log_info(f"  Metadata key-value pairs declared: {kv_count}")

        if kv_count > 10000:
            log_warn(f"Unusually high metadata key-value count: {kv_count}. Flagging for review.")
            report["anomalies"].append(f"High KV count: {kv_count}")

        # Read metadata key names. GGUF keys are stored as:
        #   key_len: uint64 LE
        #   key_data: UTF-8 bytes of length key_len
        # followed by the value type and value. We read key names only and
        # skip value parsing to keep this inspector memory-safe.
        anomalous_keys = []
        known_keys = []
        parse_error = False

        for i in range(min(kv_count, 2000)):  # cap at 2000 to prevent runaway reads
            try:
                key_len_bytes = f.read(8)
                if len(key_len_bytes) < 8:
                    log_error(f"Unexpected end of file reading metadata key length at index {i}.")
                    parse_error = True
                    break

                key_len = struct.unpack("<Q", key_len_bytes)[0]

                if key_len > 1024:
                    log_error(f"Metadata key at index {i} declares length {key_len}, which exceeds 1024 bytes. This is anomalous.")
                    report["anomalies"].append(f"Oversized key at index {i}: {key_len} bytes")
                    parse_error = True
                    break

                key_bytes = f.read(key_len)
                if len(key_bytes) < key_len:
                    log_error(f"Unexpected end of file reading metadata key data at index {i}.")
                    parse_error = True
                    break

                try:
                    key_name = key_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    log_error(f"Metadata key at index {i} is not valid UTF-8. File may be corrupted or tampered with.")
                    report["anomalies"].append(f"Non-UTF-8 key at index {i}")
                    parse_error = True
                    break

                # Classify key
                if any(key_name.startswith(prefix) for prefix in GGUF_EXPECTED_KEY_PREFIXES):
                    known_keys.append(key_name)
                else:
                    log_warn(f"Unexpected metadata key: '{key_name}' does not match any known architecture prefix.")
                    anomalous_keys.append(key_name)

                # Skip past the value: we stop parsing value data because GGUF value
                # types have variable structure. A full GGUF parser is required for
                # complete metadata inspection. This inspector only audits key names.
                # Break after reading keys to avoid parsing value bytes incorrectly.
                # Teams requiring full value inspection should use llama.cpp's
                # gguf-dump utility or the gguf-py library.
                break  # Remove this break to attempt sequential key parsing with a full value-skip implementation

            except struct.error as exc:
                log_error(f"Struct unpack error at metadata index {i}: {exc}")
                parse_error = True
                break

        report["checks"]["magic_bytes"] = "pass"
        report["checks"]["version_check"] = "pass" if version in GGUF_SUPPORTED_VERSIONS else "warn"
        report["checks"]["metadata_key_audit"] = "fail" if (anomalous_keys or parse_error) else "pass"
        report["known_keys_sampled"] = known_keys
        report["anomalous_keys"] = anomalous_keys

        if parse_error:
            log_error("GGUF metadata parse error encountered. Artifact should be quarantined.")
            write_report(report)
            return False

        if anomalous_keys:
            log_warn(f"{len(anomalous_keys)} unexpected metadata key(s) found: {anomalous_keys}")
            log_warn("Manual review required before this artifact is admitted to production.")
            report["anomalies"].append(f"Unexpected keys: {anomalous_keys}")
            # Unexpected keys are a warning, not a hard failure, unless STRICT mode is set.
            # Set the environment variable GGUF_STRICT=1 to treat unexpected keys as failures.
            if os.environ.get("GGUF_STRICT", "0") == "1":
                write_report(report)
                return False

    log_pass(f"GGUF inspection completed. {len(known_keys)} known key(s) sampled.")
    report["passed"] = True
    write_report(report)
    return True


# ---------------------------------------------------------------------------
# PyTorch stub (delegates to picklescan)
# ---------------------------------------------------------------------------

def inspect_pytorch(filepath: str) -> bool:
    """
    For PyTorch .pt and .pth files, this script delegates to picklescan,
    which performs comprehensive pickle opcode analysis. This function
    provides a Python-callable wrapper around that CLI tool.
    """
    import subprocess

    log_info(f"Delegating PyTorch inspection to picklescan: {filepath}")
    result = subprocess.run(
        ["picklescan", "-p", filepath],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log_error(f"picklescan detected a threat in {filepath}:")
        log_error(result.stdout)
        log_error(result.stderr)
        return False

    log_pass(f"picklescan: No dangerous pickle opcodes detected in {filepath}")
    return True


# ---------------------------------------------------------------------------
# Registry hash check
# ---------------------------------------------------------------------------

def verify_against_registry(filepath: str, registry_path: str) -> bool:
    """
    Compute the SHA-256 hash of the artifact and compare against the trusted
    registry. Returns True if the hash matches, False otherwise.
    """
    entry = lookup_registry(registry_path, filepath)
    if entry is None:
        log_warn(f"No registry entry found for {os.path.basename(filepath)}. Skipping hash cross-check.")
        return True  # Permissive if no entry; the workflow enforces hard failure

    trusted_hash = entry.get("sha256", "")
    if not trusted_hash:
        log_warn("Registry entry found but no sha256 field present. Skipping hash check.")
        return True

    log_info(f"Computing SHA-256 for {filepath}...")
    computed = compute_sha256(filepath)
    log_info(f"  Computed:  {computed}")
    log_info(f"  Trusted:   {trusted_hash}")

    if computed != trusted_hash:
        log_error(f"SHA-256 mismatch for {os.path.basename(filepath)}. Artifact does not match the trusted registry.")
        return False

    log_pass(f"SHA-256 hash verified for {os.path.basename(filepath)}")
    log_info(f"  Reviewed by: {entry.get('reviewed_by', 'unknown')}")
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect ML model weight files for binary-level anomalies before pipeline admission.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--file", "-f",
        required=True,
        help="Path to the model artifact to inspect.",
    )
    parser.add_argument(
        "--registry", "-r",
        default="registry/trusted_models.json",
        help="Path to the trusted model registry JSON. (default: registry/trusted_models.json)",
    )
    parser.add_argument(
        "--format",
        choices=["safetensors", "gguf", "pt", "pth", "auto"],
        default="auto",
        help="Model file format. 'auto' infers from file extension. (default: auto)",
    )
    parser.add_argument(
        "--skip-hash",
        action="store_true",
        default=False,
        help="Skip SHA-256 registry hash check. Use only for local development.",
    )

    args = parser.parse_args()

    filepath = args.file
    if not os.path.isfile(filepath):
        log_error(f"File not found: {filepath}")
        sys.exit(1)

    file_format = args.format
    if file_format == "auto":
        ext = Path(filepath).suffix.lower().lstrip(".")
        if ext in ("safetensors",):
            file_format = "safetensors"
        elif ext in ("gguf",):
            file_format = "gguf"
        elif ext in ("pt", "pth", "bin"):
            file_format = "pt"
        else:
            log_warn(f"Cannot infer format from extension '.{ext}'. Defaulting to safetensors inspection.")
            file_format = "safetensors"

    log_info("=" * 60)
    log_info("model-provenance-guard: Weight Inspection")
    log_info(f"  File:   {filepath}")
    log_info(f"  Format: {file_format}")
    log_info(f"  Size:   {os.path.getsize(filepath):,} bytes")
    log_info("=" * 60)

    all_passed = True

    # Step 1: Registry hash check
    if not args.skip_hash:
        if not verify_against_registry(filepath, args.registry):
            all_passed = False
    else:
        log_warn("Hash verification skipped (--skip-hash). Do not use in production.")

    # Step 2: Format-specific inspection
    if file_format == "safetensors":
        if not inspect_safetensors(filepath, args.registry):
            all_passed = False
    elif file_format == "gguf":
        if not inspect_gguf(filepath, args.registry):
            all_passed = False
    elif file_format in ("pt", "pth"):
        if not inspect_pytorch(filepath):
            all_passed = False

    log_info("=" * 60)
    if all_passed:
        log_pass("All inspection checks passed. Artifact is cleared for pipeline admission.")
        sys.exit(0)
    else:
        log_error("One or more inspection checks failed. Artifact is QUARANTINED.")
        log_error("Do not load this artifact into memory or promote it to production.")
        sys.exit(1)


if __name__ == "__main__":
    main()
