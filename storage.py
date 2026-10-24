"""Private local and S3-compatible object storage adapters.

Local storage remains the default. R2 object keys are tenant scoped and only
reached through authorization in the Flask application before a short-lived
signed read URL is issued.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote


R2_FILENAME_PREFIX = "r2__"


class StorageConfigurationError(RuntimeError):
    pass


def cloud_storage_enabled() -> bool:
    return os.environ.get("STORAGE_BACKEND", "local").strip().lower() == "r2"


class R2Storage:
    def __init__(self, client=None, bucket: str | None = None):
        self.bucket = bucket or os.environ.get("R2_BUCKET_NAME", "").strip()
        if not self.bucket:
            raise StorageConfigurationError("R2_BUCKET_NAME must be configured before R2 storage is enabled.")
        if client is None:
            required = ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
            missing = [name for name in required if not os.environ.get(name, "").strip()]
            if not os.environ.get("R2_ENDPOINT_URL", "").strip() and not os.environ.get("R2_ACCOUNT_ID", "").strip():
                missing.append("R2_ENDPOINT_URL or R2_ACCOUNT_ID")
            if missing:
                raise StorageConfigurationError("Missing private R2 settings: " + ", ".join(missing))
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:
                raise StorageConfigurationError("Install the boto3 package to enable R2 storage.") from exc
            client = boto3.client(
                "s3",
                endpoint_url=os.environ.get("R2_ENDPOINT_URL", "").strip() or
                    f"https://{os.environ['R2_ACCOUNT_ID'].strip()}.r2.cloudflarestorage.com",
                aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                region_name="auto",
                config=Config(
                    signature_version="s3v4",
                    retries={"max_attempts": 5, "mode": "standard"},
                    s3={"addressing_style": "path"},
                ),
            )
        self.client = client

    def upload_file(self, path: Path, key: str, content_type: str = "application/octet-stream") -> None:
        """Use boto3 managed multipart upload for large files and retryable parts."""
        try:
            from boto3.s3.transfer import TransferConfig
        except ImportError as exc:
            raise StorageConfigurationError("Install the boto3 package to enable R2 transfers.") from exc
        transfer = TransferConfig(
            multipart_threshold=64 * 1024 * 1024,
            multipart_chunksize=64 * 1024 * 1024,
            max_concurrency=4,
            use_threads=True,
        )
        self.client.upload_file(
            str(path), self.bucket, key,
            ExtraArgs={"ContentType": content_type},
            Config=transfer,
        )

    def start_multipart(self, key: str, content_type: str) -> str:
        response = self.client.create_multipart_upload(
            Bucket=self.bucket, Key=key, ContentType=content_type,
        )
        return response["UploadId"]

    def signed_upload_part(self, key: str, upload_id: str, part_number: int, expires: int = 900) -> str:
        return self.client.generate_presigned_url(
            "upload_part",
            Params={"Bucket": self.bucket, "Key": key, "UploadId": upload_id, "PartNumber": part_number},
            ExpiresIn=expires,
        )

    def list_multipart_parts(self, key: str, upload_id: str) -> list[dict]:
        parts: list[dict] = []
        marker = 0
        while True:
            response = self.client.list_parts(
                Bucket=self.bucket, Key=key, UploadId=upload_id, PartNumberMarker=marker, MaxParts=1000,
            )
            parts.extend(response.get("Parts", []))
            if not response.get("IsTruncated"):
                return parts
            marker = response.get("NextPartNumberMarker", parts[-1]["PartNumber"] if parts else marker)

    def complete_multipart(self, key: str, upload_id: str, parts: list[dict]) -> None:
        self.client.complete_multipart_upload(
            Bucket=self.bucket, Key=key, UploadId=upload_id,
            MultipartUpload={"Parts": [{"PartNumber": part["PartNumber"], "ETag": part["ETag"]}
                                        for part in parts]},
        )

    def abort_multipart(self, key: str, upload_id: str) -> None:
        self.client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)

    def object_size(self, key: str) -> int:
        return int(self.client.head_object(Bucket=self.bucket, Key=key)["ContentLength"])

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def signed_download(self, key: str, filename: str, content_type: str, download: bool, expires: int = 300) -> str:
        disposition = "attachment" if download else "inline"
        safe_name = quote(Path(filename).name, safe="")
        return self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentType": content_type,
                "ResponseContentDisposition": f"{disposition}; filename*=UTF-8''{safe_name}",
            },
            ExpiresIn=expires,
        )


def is_r2_filename(filename: str) -> bool:
    return (filename or "").startswith(R2_FILENAME_PREFIX)


def r2_object_key(filename: str) -> str:
    if not is_r2_filename(filename):
        raise ValueError("Not an R2-backed filename")
    return filename[len(R2_FILENAME_PREFIX):]
