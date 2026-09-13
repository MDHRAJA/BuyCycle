## Final full-dataset run

The final run used the local deterministic forecasting engine in `code/main.py`
and a local RapidOCR ONNX inference runtime for image-backed amounts. It made no
hosted provider/model calls and therefore used 0 input tokens, 0 output tokens,
and had an estimated cost of $0.00 ($0.00 per request). The final run processed
250 requests; average hosted-model tokens per request were 0.

Image evidence was opened locally during ingestion to confirm available media.
No external OCR or model service was used.
