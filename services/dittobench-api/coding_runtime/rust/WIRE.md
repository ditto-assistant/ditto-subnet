# Bounded Rust data channel

This layer supplies a typed binary codec, a one-request-at-a-time session, and
deadline-aware Unix-stream I/O. It does not launch a compiler or candidate,
authenticate a peer, implement `CandidateApi`, certify process cleanup, or emit
a grading report. No runtime or catalog is activated.

## Private authority stays in the parent

The caller binds the fixed ordered function/signature table to its approved
program, bridge, and compiled artifact outside this channel. Function indices
are table positions, never reflection requests or candidate-selected authority.
Session construction rejects empty/oversized tables and invalid schemas.

Private program/source digests, test names, expected values, and pass/fail counts never
enter frames. A session gets a fresh OS-random 32-byte identifier; every call gets
another fresh 32-byte challenge. Randomness failure has no deterministic fallback.
These are correlation tokens, not peer authentication or stable task identities.

Only a matching session, unpredictable challenge, and sequence number can complete
the single pending request. There are at most 128 requests per session. Concurrent
issuance, unsolicited replies, replay, malformed packets, and candidate-failure
replies permanently poison the session. The channel closes its socket on failure.
A domain `Result::Err` remains ordinary typed data; a candidate cannot request an
infrastructure retry by selecting an error string or a report opcode.

## Frame format, version 1

All fixed-width numbers are big-endian:

```text
u32(body length) | "DRW1" | kind:u8 | session:32 bytes |
challenge:32 bytes | sequence:u64 | payload
```

The body is 77–65536 bytes, excluding its four-byte length prefix. Kind 0 is a
request, 1 a value response, and 2 a candidate-failure response. Other versions
or kinds are rejected. Failure responses have no payload. Requests carry:

```text
function:u16 | argument_count:u8 | (value_length:u32 | typed_value)*
```

The fixed table defines argument count/types and return type. Responses carry
only the return value's bytes, not a peer-supplied type description. Both request
and response decoding require exact consumption, including nested arguments.

| Value | Encoding under the independently known type |
|---|---|
| Boolean | One byte, exactly 0 or 1 |
| Integer | Signed 128-bit representation; declared signedness/width range checked |
| Character | Unicode scalar as u32; surrogates and invalid scalars rejected |
| Text | u32 byte length followed by strictly valid UTF-8 |
| Vector/slice | u32 count followed by elements |
| Array/tuple | Exactly the schema-defined elements; no peer-defined shape |
| Option | 0 for None; 1 followed by the Some value |
| Result | 0 followed by Ok; 1 followed by Err |
| Reference description | The schema-defined referent; never a process address |

The decoder checks lengths and a shared 4096-node/64-KiB charged-data budget
before allocating collections or copying text. All arguments share that budget,
including zero-byte unit elements. Value schema validation retains its depth,
shape, and integer checks. Frame limits can be stricter than standalone value
limits once envelope overhead is added. No floating-point integer conversion,
generic object decoding, or score/report deserialization exists.

## Unix I/O and remaining boundary

The parent owns an exclusive, already-connected private socket. It validates the
four-byte prefix before body allocation. Every partial read and write uses the
remaining time until the same absolute deadline; slow-drip traffic cannot reset
that deadline. EOF, malformed lengths, expired deadlines, and I/O failures stop
the exchange. No in-stream resynchronization or protocol fallback is attempted.

The caller must verify the peer/process and approved bridge identity before
sending inputs. It must still impose an outer wall-clock/process-resource guard,
including OS entropy acquisition, scheduling and bounded decoding work. This
module does not set up sandboxing or classify launch-stage versus candidate-stage
I/O faults. The separate session and framing APIs let that adapter preserve fault
provenance. Closing a socket never proves termination or reaping. The separate
[process adapter](RUNTIME.md) owns and verifies those operations before returning
`CandidateApi::finish()` success; `Channel` itself still cannot certify cleanup.

Current tests use public synthetic values and local Unix socket pairs. They cover
malformed values/frames, replay and cross-session responses, shared budgets,
fragmentation, slow-drip reads, and blocked writes. They do not qualify a private
Rust suite, a compiled candidate, a native host, or a deployed canary.
