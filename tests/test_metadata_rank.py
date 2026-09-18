from shared.metadata_rank import MetadataFeatures, metadata_features


def test_sequence_exact_match_and_conflict():
    assert metadata_features("붙임 2 서식", "붙임2 신청 서식").sequence == 1
    assert metadata_features("제6회 심의", "제5회 심의").sequence == -2
    assert metadata_features("2차 모집", "1차 모집").sequence == -2


def test_document_kind_match_and_conflict():
    assert metadata_features("신청 양식", "멘토링 신청양식.hwp").document_kind == 1
    assert metadata_features("결과 보고서", "운영 계획.hwp").document_kind == -1
    assert metadata_features("지원 대상", "운영 계획.hwp").document_kind == 0


def test_revision_bonus_requires_revision_intent():
    assert metadata_features("개정된 규정", "규정 일부개정 최종.hwp").revision == 1
    assert metadata_features("규정 내용", "규정 일부개정 최종.hwp").revision == 0


def test_combined_features_are_independent():
    assert metadata_features("제2회 개정 신청 양식", "제2회 개정 신청양식") == MetadataFeatures(
        sequence=1,
        document_kind=1,
        revision=1,
    )
