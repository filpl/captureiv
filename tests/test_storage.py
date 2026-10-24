import unittest

from storage import R2Storage, R2_FILENAME_PREFIX, is_r2_filename, r2_object_key


class FakeS3Client:
    def __init__(self):
        self.calls = []

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.calls.append((operation, Params, ExpiresIn))
        return "https://private-object.test/signed"

    def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))

    def create_multipart_upload(self, **kwargs):
        self.calls.append(("create_multipart_upload", kwargs))
        return {"UploadId": "multipart-1"}

    def list_parts(self, **kwargs):
        self.calls.append(("list_parts", kwargs))
        return {"Parts": [{"PartNumber": 1, "ETag": '"part"', "Size": 5}], "IsTruncated": False}

    def complete_multipart_upload(self, **kwargs):
        self.calls.append(("complete_multipart_upload", kwargs))

    def abort_multipart_upload(self, **kwargs):
        self.calls.append(("abort_multipart_upload", kwargs))

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        return {"ContentLength": 5}


class R2StorageTests(unittest.TestCase):
    def test_private_signed_read_is_short_lived_and_supports_inline_or_download(self):
        client = FakeS3Client()
        storage = R2Storage(client=client, bucket="private-bucket")
        url = storage.signed_download("companies/12/object.mp4", "flight footage.mp4", "video/mp4", False)
        self.assertEqual(url, "https://private-object.test/signed")
        operation, params, expiry = client.calls[-1]
        self.assertEqual(operation, "get_object")
        self.assertEqual(expiry, 300)
        self.assertEqual(params["ResponseContentType"], "video/mp4")
        self.assertIn("inline;", params["ResponseContentDisposition"])
        storage.signed_download("companies/12/object.mp4", "flight footage.mp4", "video/mp4", True)
        self.assertIn("attachment;", client.calls[-1][1]["ResponseContentDisposition"])

    def test_cloud_filename_keeps_local_paths_distinguishable(self):
        filename = R2_FILENAME_PREFIX + "companies/12/object.mp4"
        self.assertTrue(is_r2_filename(filename))
        self.assertEqual(r2_object_key(filename), "companies/12/object.mp4")
        self.assertFalse(is_r2_filename("random_local_upload.mp4"))

    def test_object_delete_targets_the_private_bucket_key(self):
        client = FakeS3Client()
        R2Storage(client=client, bucket="private-bucket").delete("companies/12/object.mp4")
        self.assertEqual(client.calls[-1], ("delete_object", {"Bucket": "private-bucket", "Key": "companies/12/object.mp4"}))

    def test_resumable_multipart_storage_operations_are_bucket_scoped(self):
        client = FakeS3Client()
        storage = R2Storage(client=client, bucket="private-bucket")
        self.assertEqual(storage.start_multipart("companies/12/video.mp4", "video/mp4"), "multipart-1")
        self.assertEqual(storage.signed_upload_part("companies/12/video.mp4", "multipart-1", 1),
                         "https://private-object.test/signed")
        parts = storage.list_multipart_parts("companies/12/video.mp4", "multipart-1")
        self.assertEqual(parts[0]["Size"], 5)
        storage.complete_multipart("companies/12/video.mp4", "multipart-1", parts)
        self.assertEqual(storage.object_size("companies/12/video.mp4"), 5)
        storage.abort_multipart("companies/12/video.mp4", "multipart-1")
        self.assertEqual([call[0] for call in client.calls], [
            "create_multipart_upload", "upload_part", "list_parts", "complete_multipart_upload",
            "head_object", "abort_multipart_upload",
        ])


if __name__ == "__main__":
    unittest.main()
