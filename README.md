# Valgo API

The official Python client for the Valgo API to upload, share, and retrieve datasets programmatically.

## Installation

```bash
pip install valgo
```

Valgo supports Python 3.11 and newer.

## Quick start

Set the API key issued by your Valgo administrator:

```bash
export VALGO_API_KEY="valgo_live_..."
```

Upload a file:

```python
from valgo import Valgo

client = Valgo()
uploaded = client.upload("data/dataset.parquet")
print(uploaded.artifact_id)
```

The SDK connects to `https://api.valgo.ai` by default.

You can also pass the key directly:

```python
client = Valgo(api_key="valgo_live_...")
```

## Uploads

Upload a file with an optional logical name and metadata:

```python
uploaded = client.upload(
    "data/dataset.parquet",
    name="datasets/dataset.parquet",
    metadata={"source": "example"},
)
```

Directories are uploaded concurrently:

```python
result = client.upload("data/datasets")

for uploaded in result.completed:
    print(uploaded.path, uploaded.artifact_id)

for failure in result.failures:
    print(failure.path, failure.error)
```

Interrupted uploads are resumable. Retrying the same file continues the existing transfer rather than creating a duplicate.

## Downloads

Download the latest version by logical name:

```python
client.download("datasets/dataset.parquet", "downloads/dataset.parquet")
```

Or retrieve an exact immutable version:

```python
client.download(uploaded.artifact_id, "downloads/dataset.parquet")
```

The SDK verifies the downloaded size and SHA-256 checksum before replacing the destination file.

## Configuration

| Environment variable | Purpose | Default |
| --- | --- | --- |
| `VALGO_API_KEY` | Customer API credential | Required |
| `VALGO_BASE_URL` | Valgo API endpoint | `https://api.valgo.ai` |

Constructor arguments override environment variables:

```python
client = Valgo(
    api_key="valgo_live_...",
    base_url="https://api.valgo.ai",
    timeout=60,
    max_workers=8,
)
```

## Development

From this repository:

```bash
python -m pip install -e .
pytest
```

Do not commit API keys, presigned URLs, or customer data. Report security issues privately to the Valgo team rather than opening a public issue.

## License

Licensed under the [Apache License 2.0](LICENSE).

Copyright © 2026 Valgorithmic, Inc. (d.b.a. Valgo).
