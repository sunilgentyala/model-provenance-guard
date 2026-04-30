# Threat Model: ML Model Weight File Supply Chain

**Version:** 1.0
**Author:** Sunil Gentyala
**Repository:** github.com/sunilgentyala/model-provenance-guard

---

## Scope

This document covers the attack surface of three ML model weight file formats commonly used in enterprise inference deployments: PyTorch checkpoints (.pt, .pth), Safetensors (.safetensors), and GGUF (.gguf). It does not cover prompt injection, model output manipulation, or training data poisoning at the data collection layer.

---

## Format Threat Analysis

### PyTorch Checkpoints (.pt, .pth)

**Serialization mechanism:** Python pickle protocol.

**Critical vulnerability:** The pickle protocol supports a `__reduce__` method on Python objects. A serialized checkpoint that includes an object with a custom `__reduce__` implementation will execute that method's return value as a Python callable with arguments during deserialization. This occurs the moment `torch.load()` is called, before any application code can inspect the loaded data.

**Attack scenario:** An adversary who obtains write access to a model repository (through a compromised account, a malicious pull request merged by an inattentive maintainer, or a fork-and-publish pattern) modifies a checkpoint to include a malicious object. When an ML engineer runs `torch.load("model.pt")` in their training or inference environment, the payload executes. Common payloads observed in published research include reverse shells, credential harvesters targeting cloud provider metadata endpoints, and persistence mechanisms that inject into the Python process.

**Controls in this toolkit:** picklescan pre-scan before any load operation.

**Residual risk:** picklescan detects known dangerous opcodes. Novel serialization patterns designed to evade opcode-level scanning require sandbox execution analysis, which is outside the scope of this toolkit.

---

### Safetensors (.safetensors)

**Serialization mechanism:** JSON header followed by raw tensor byte data. No Python deserialization.

**Design intent:** Eliminate arbitrary code execution at load time. This goal is achieved for the tensor loading operation itself.

**Remaining attack surfaces:**

1. **Header parser exploits.** The JSON header is parsed before tensor data is read. Maliciously crafted header values can crash or exploit header parsing code in consumers. Oversized headers, invalid Unicode sequences, and deeply nested JSON structures are all valid attack vectors against parser implementations.

2. **Metadata field abuse.** The `__metadata__` dictionary in the header accepts arbitrary string key-value pairs with no schema enforcement. Metadata fields cannot execute code directly, but they can be used to exfiltrate information about the consuming environment if the inference application logs or forwards metadata values without sanitization.

3. **Neural backdoor embedding.** The tensor data itself can contain weights that have been modified through a backdoor attack (BadNets, TrojAI-style trojans, or RLHF-based poisoning). The model will pass all standard format checks and load correctly. The backdoor activates only on inputs containing a specific trigger pattern. Detection requires behavioral analysis at inference time, not binary inspection.

**Controls in this toolkit:** Header size validation, JSON parse validation, dtype and offset integrity checks, `__metadata__` type enforcement.

**Residual risk:** Neural backdoors in tensor data are not detectable by binary inspection. Behavioral monitoring at inference time is the required control.

---

### GGUF (.gguf)

**Serialization mechanism:** Custom binary format with a typed key-value metadata section followed by tensor data. Used by llama.cpp and compatible runtimes.

**Attack surfaces:**

1. **Metadata key-value injection.** The metadata section has no schema enforcement in the format specification beyond type tagging. Nonstandard keys are silently accepted by most consuming libraries. A threat actor can embed arbitrary data in custom metadata keys. In C/C++ consumers, keys or values that exceed expected length boundaries can trigger buffer handling issues in implementations that do not perform rigorous bounds checking.

2. **Version field manipulation.** The version field at bytes 4-7 controls how the rest of the file is parsed. Setting an unsupported version value can trigger undefined parsing behavior in consuming libraries.

3. **Tensor count manipulation.** Declaring a tensor count (bytes 8-15) that differs from the actual number of tensors in the file can cause consumers to over-read memory during model loading.

4. **Neural backdoor embedding.** Same residual risk as Safetensors: quantized weights can carry poisoned parameters that activate on trigger inputs. Binary inspection cannot detect this.

**Controls in this toolkit:** Magic byte validation, version field check, metadata key prefix audit, key length bounds checking.

**Residual risk:** Full GGUF value-level inspection requires a complete GGUF parser. This toolkit inspects key names only for the metadata section. Use `gguf-py` or llama.cpp's `gguf-dump` for full metadata value inspection.

---

## Controls Summary

| Threat | Format | Control | Toolkit Coverage |
|---|---|---|---|
| Pickle arbitrary code execution | .pt, .pth | picklescan pre-scan | Full |
| Oversized / malformed header | .safetensors | Header size and JSON validation | Full |
| Invalid tensor dtype or offsets | .safetensors | verify_weights.py tensor entry checks | Full |
| Metadata type confusion | .safetensors | __metadata__ type enforcement | Full |
| Invalid magic bytes | .gguf | Magic byte check | Full |
| Unsupported version | .gguf | Version field check | Full |
| Nonstandard metadata keys | .gguf | Key prefix audit | Partial (key names only) |
| Oversized metadata key length | .gguf | Key length bounds check | Full |
| Neural backdoor in weights | All | Behavioral inference monitoring | Not covered (out of scope) |
| Hash mismatch (supply chain swap) | All | SHA-256 vs trusted registry | Full |
| Missing cryptographic signature | All | Cosign signature verification | Full |

---

## Out of Scope

- ONNX format (.onnx): ONNX uses Protocol Buffers and has its own vulnerability profile. A separate inspector is needed.
- TensorFlow SavedModel: The SavedModel format includes a `saved_model.pb` protobuf and Python function assets. Inspection requires protobuf parsing and asset auditing.
- Training data poisoning: Attacks at the data collection or training stage that produce backdoored weights through legitimate training procedures.
- Inference-time prompt injection: Attacks that manipulate model behavior through adversarial inputs rather than weight modification.

---

## References

- GGUF Specification: https://github.com/ggerganov/ggml/blob/master/docs/gguf.md
- Safetensors Specification: https://huggingface.co/docs/safetensors/index
- Sigstore Cosign: https://docs.sigstore.dev/cosign/overview
- picklescan: https://github.com/mmaitre314/picklescan
- MITRE ATLAS: https://atlas.mitre.org (ML-specific threat taxonomy)
- NIST AI RMF: https://airc.nist.gov/RMF_Overview
