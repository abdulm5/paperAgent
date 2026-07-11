import hashlib
import json
import math
import re
from typing import Any

CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def tokenize(*values: object) -> set[str]:
    text = " ".join(str(value) for value in values if value is not None)
    text = CAMEL_BOUNDARY.sub(" ", text).replace("_", " ").replace("/", " ").lower()
    return {token for token in TOKEN_PATTERN.findall(text) if len(token) > 1}


def canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def token_coverage(query: set[str], document: set[str]) -> float:
    return len(query & document) / len(query) if query else 0.0


def hashed_vector(tokens: set[str], dimensions: int = 64) -> list[float]:
    vector = [0.0] * dimensions
    for token in tokens:
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:2], "big") % dimensions
        sign = 1.0 if digest[2] % 2 == 0 else -1.0
        vector[index] += sign
    return vector


def cosine_similarity(left_tokens: set[str], right_tokens: set[str]) -> float:
    left = hashed_vector(left_tokens)
    right = hashed_vector(right_tokens)
    dot_product = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return max(0.0, dot_product / (left_norm * right_norm))
