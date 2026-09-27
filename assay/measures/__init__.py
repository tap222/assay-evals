from assay.measures.base import Measure, MeasureOutput, SliceResult
from assay.measures.cost import CostPerDocument, CostPerPage, HumanTouchRate, TotalSpend
from assay.measures.documents import (LocationAccuracy, OcrCharacterErrors, OcrDigitErrors, OcrReadingOrder,
                                      TableCellAccuracy)
from assay.measures.errors import ErrorsByOrigin, PromptErrorRate, ReportedErrorRate
from assay.measures.ground_truth import EscapeRate, FieldAccuracy, SplitStraightThrough, SupersededValues
from assay.measures.operations import (CallErrorRate, CallLatencyP95, DocumentVolume, InputMixDrift,
                                       StageFailureRate)
from assay.measures.pipeline import (CostCoverage, FallbackAttribution, HandoffLoss, ModelMismatch,
                                     NoOpStages, RevisionCoverage, SourcePositions, TimeToComplete)

REGISTRY = {m.id: m for m in [
    # operational health
    DocumentVolume(), StageFailureRate(), CallErrorRate(), CallLatencyP95(), TimeToComplete(),
    InputMixDrift(),
    # cost
    CostPerDocument(), CostPerPage(), TotalSpend(), HumanTouchRate(),
    # pipeline integrity
    FallbackAttribution(), ModelMismatch(), CostCoverage(), RevisionCoverage(),
    NoOpStages(), SourcePositions(), HandoffLoss(),
    # error analysis
    ReportedErrorRate(), ErrorsByOrigin(), PromptErrorRate(),
    # needs ground truth
    SplitStraightThrough(), FieldAccuracy(), SupersededValues(), EscapeRate(),
    # document quality, from scored checks
    OcrCharacterErrors(), OcrDigitErrors(), OcrReadingOrder(), LocationAccuracy(), TableCellAccuracy(),
]}

GROUPS = {
    "Operational health": ["document_volume", "stage_failure_rate", "call_error_rate",
                           "call_latency_p95", "time_to_complete_p90", "input_mix_drift"],
    "Cost": ["cost_per_document", "cost_per_page", "total_spend", "human_touch_rate", "cost_coverage"],
    "Pipeline integrity": ["fallback_attribution", "model_mismatch", "revision_coverage",
                           "noop_stage_rate", "source_positions", "handoff_loss"],
    "Errors": ["reported_error_rate", "errors_by_origin", "prompt_error_rate"],
    "Accuracy (needs ground truth)": ["split_stp", "field_accuracy", "superseded_value_rate", "escape_rate"],
    "Document quality": ["ocr_cer", "ocr_digit_error_rate", "ocr_reading_order", "location_accuracy",
                         "table_cell_f1"],
}

__all__ = ["REGISTRY", "GROUPS", "Measure", "MeasureOutput", "SliceResult"]
