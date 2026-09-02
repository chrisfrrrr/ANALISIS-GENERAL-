from models.config import RiskConfig
from services.analysis_service import AnalysisService
from utils.course_structure import global_week, split_global_week, week_label


def test_module_week_conversion():
    assert global_week(1, 1) == 1
    assert global_week(2, 1) == 8
    assert global_week(3, 7) == 21
    assert split_global_week(14) == (2, 7)
    assert split_global_week(15) == (3, 1)


def test_parallel_config_is_always_21_weeks():
    assert RiskConfig().course_weeks == 21
    assert RiskConfig.from_dict({"course_weeks": 5}).course_weeks == 21


def test_plan_week_reads_leveling_label():
    label = week_label(9, include_global=True)
    assert AnalysisService._plan_week(label, 21) == 9
