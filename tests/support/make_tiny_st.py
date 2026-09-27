"""Generate `tests/fixtures/models/tiny-st/` (impl 03 §11): a tiny sentence-transformers model.

A 1-layer BERT-like transformer (hidden 16) + mean pooling + a linear projection to 1024
dimensions, safetensors weights only, a local WordPiece vocabulary; built offline from a fixed
seed, so re-running it reproduces the committed files. It stands in for bge-m3 in CPU encoder
tests (T03-06). Run: `uv run python -m tests.support.make_tiny_st [out_dir]`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[2]
TINY_ST: Final = ROOT / "tests" / "fixtures" / "models" / "tiny-st"
HIDDEN: Final = 16
OUT_DIM: Final = 1024
_SPECIAL: Final = ("[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]")
_WORDS: Final = (  # split in _vocab
    "the a of to and in is on for with at by from not no error fail failed failure "
    "disk network server database login password reset slow timeout outage printer "
    "email vpn service restart update patch change incident problem user access "
    "denied memory cpu storage backup job batch queue report ticket high low urgent"
)
_KEEP: Final = frozenset(
    {
        "config.json",
        "config_sentence_transformers.json",
        "modules.json",
        "sentence_bert_config.json",
        "model.safetensors",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "vocab.txt",
    }
)


def _vocab() -> list[str]:
    chars = [chr(c) for c in range(ord("a"), ord("z") + 1)] + list("0123456789.,:;-_/!?'")
    pieces = [f"##{c}" for c in chars]
    return [*_SPECIAL, *chars, *pieces, *_WORDS.split()]


def _tidy(out: Path) -> None:
    """Drop files outside `_KEEP` (model card, etc.); end text files with exactly one newline."""
    for path in sorted(out.rglob("*"), reverse=True):
        if path.is_dir():
            if not any(path.iterdir()):
                path.rmdir()
            continue
        if path.name not in _KEEP:
            path.unlink()
        elif path.suffix in {".json", ".txt"}:
            lines = path.read_text(encoding="utf-8").splitlines()
            text = "\n".join(line.rstrip() for line in lines).rstrip("\n") + "\n"
            path.write_bytes(text.encode("utf-8"))


def build(out: Path = TINY_ST) -> Path:
    """Write the tiny model to `out` (replacing its files) and return `out`."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    import torch  # noqa: PLC0415 - heavy imports only when generating
    from sentence_transformers import SentenceTransformer  # noqa: PLC0415
    from sentence_transformers.sentence_transformer import modules  # noqa: PLC0415
    from transformers import BertConfig, BertModel, BertTokenizerFast  # noqa: PLC0415

    torch.manual_seed(0)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        vocab_file = base / "vocab.txt"
        vocab_file.write_text("\n".join(_vocab()) + "\n", encoding="utf-8")
        tokenizer = BertTokenizerFast(vocab_file=str(vocab_file), do_lower_case=True)
        config = BertConfig(
            vocab_size=len(_vocab()),
            hidden_size=HIDDEN,
            num_hidden_layers=1,
            num_attention_heads=2,
            intermediate_size=32,
            max_position_embeddings=512,
        )
        BertModel(config).save_pretrained(base / "bert", safe_serialization=True)
        tokenizer.save_pretrained(base / "bert")
        transformer = modules.Transformer(str(base / "bert"), max_seq_length=512)
        pooling = modules.Pooling(HIDDEN, pooling_mode="mean")
        dense = modules.Dense(HIDDEN, OUT_DIM, activation_function=torch.nn.Identity())
        model = SentenceTransformer(modules=[transformer, pooling, dense], device="cpu")
        out.mkdir(parents=True, exist_ok=True)
        model.save(str(out), safe_serialization=True)
    _tidy(out)
    return out


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else TINY_ST
    sys.stdout.write(f"{build(target)}\n")
