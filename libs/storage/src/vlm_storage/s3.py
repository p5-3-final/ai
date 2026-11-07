"""S3 API 구현(운영 S3, 로컬 SeaweedFS)."""

from pathlib import Path
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic_settings import BaseSettings, SettingsConfigDict

from vlm_storage.storage import ObjectNotFoundError, check_expiry

if TYPE_CHECKING:
    # 타입 스텁은 개발 의존성이라 운영 이미지에는 없다.
    from types_boto3_s3 import S3Client

# S3 DeleteObjects 한 번에 지울 수 있는 최대 키 수
_DELETE_BATCH = 1000
_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


class StorageSettings(BaseSettings):
    """저장소 설정. 환경변수 `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_REGION`, `S3_PATH_STYLE`.

    자격 증명은 여기서 받지 않고 boto3 기본 체인(운영은 IRSA, 로컬은 `AWS_ACCESS_KEY_ID` 등)을 쓴다.
    """

    model_config = SettingsConfigDict(env_prefix="S3_", frozen=True, extra="ignore")

    bucket: str
    # 비우면 AWS S3. 로컬은 SeaweedFS 주소(http://seaweedfs:8333)다.
    endpoint_url: str | None = None
    region: str = "us-east-1"
    # SeaweedFS는 버킷 이름을 호스트에 붙이는 가상 호스트 방식을 지원하지 않는다.
    path_style: bool = False


class S3Storage:
    """`ObjectStorage`의 S3 API 구현."""

    def __init__(self, settings: StorageSettings) -> None:
        self._bucket = settings.bucket
        self._client: S3Client = boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            region_name=settings.region,
            config=Config(
                s3={"addressing_style": "path" if settings.path_style else "auto"},
                retries={"mode": "standard"},
            ),
        )

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)

    def put_bytes_if_absent(self, key: str, data: bytes, content_type: str) -> bool:
        """최초 이벤트 스냅샷을 원자적으로 보존한다. 다른 오류는 재시도를 위해 전파한다."""
        try:
            self._client.put_object(
                Bucket=self._bucket, Key=key, Body=data, ContentType=content_type, IfNoneMatch="*"
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "PreconditionFailed":
                return False
            raise
        return True

    def put_file(self, key: str, path: Path, content_type: str) -> None:
        self._client.upload_file(
            str(path), self._bucket, key, ExtraArgs={"ContentType": content_type}
        )

    def get_bytes(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as e:
            raise _translate(e, key) from e
        return response["Body"].read()

    def get_file(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._client.download_file(self._bucket, key, str(path))
        except ClientError as e:
            raise _translate(e, key) from e

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as e:
            if _is_not_found(e):
                return False
            raise
        return True

    def delete_prefix(self, prefix: str) -> int:
        deleted = 0
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            keys = [obj["Key"] for obj in page.get("Contents", [])]
            for start in range(0, len(keys), _DELETE_BATCH):
                batch = keys[start : start + _DELETE_BATCH]
                self._client.delete_objects(
                    Bucket=self._bucket,
                    Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
                )
                deleted += len(batch)
        return deleted

    def presigned_get_url(self, key: str, *, expires_s: int) -> str:
        check_expiry(expires_s)
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self._bucket, "Key": key}, ExpiresIn=expires_s
        )


def _is_not_found(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code", "") in _NOT_FOUND_CODES


def _translate(error: ClientError, key: str) -> Exception:
    if _is_not_found(error):
        return ObjectNotFoundError(f"object not found: {key}")
    return error
