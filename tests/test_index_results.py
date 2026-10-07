from shared.index_results import classify_files, summary


def classify(evidence, parts=None, version=True, audience="STUDENT"):
    job = {
        "fileIds": ["document"],
        "gcsUris": ["gs://b/document.docx", "gs://b/document.meta.md"],
        "expectedParts": ["faculty", "student"],
    }
    return classify_files(
        job,
        parts or {},
        {"document": {"name": "doc", "audience": audience}},
        evidence,
        version_matches={"document": version},
    )[0]


def test_metadata_success_does_not_cover_body_failure():
    e = {(p, "gs://b/document.meta.md"): {"status": "DONE"} for p in ("faculty", "student")}
    e[("faculty", "gs://b/document.docx")] = {"status": "FAILED", "error": "File is empty"}
    assert classify(e)["status"] == "FAILED"


def test_all_objects_and_both_corpora_required():
    e = {
        (p, "gs://b/document" + suffix): {"status": "DONE"}
        for p in ("faculty", "student")
        for suffix in (".docx", ".meta.md")
    }
    assert classify(e)["status"] == "DONE"
    del e[("student", "gs://b/document.docx")]
    assert classify(e)["status"] == "PARTIAL"
    assert classify(e, version=False)["status"] == "UNKNOWN"


def test_staff_student_cleanup_is_not_assumed_successful():
    e = {
        ("faculty", "gs://b/document" + suffix): {"status": "DONE"}
        for suffix in (".docx", ".meta.md")
    }
    assert classify(e, audience="STAFF")["status"] == "PARTIAL"
    assert classify(e, parts={"student": {"status": "DONE"}}, audience="STAFF")["status"] == "DONE"


def test_file_checkpoints_and_counts_are_separate_from_batch_status():
    parts = {
        p: {"status": "RETRYING", "fileResults": {"document": {"status": "DONE"}}}
        for p in ("faculty", "student")
    }
    result = classify({}, parts=parts, version=False)
    assert result["status"] == "DONE"
    assert summary([result]) == {"DONE": 1, "FAILED": 0, "PARTIAL": 0, "UNKNOWN": 0}
