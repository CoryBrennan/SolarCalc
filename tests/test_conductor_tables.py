from app.conductor_tables import (
    equipment_grounding_conductor_size,
    grounding_conductor_size,
    next_standard_size,
    select_conductor,
)


def test_select_conductor_75c():
    assert select_conductor(35, 75) == "10 AWG"
    assert select_conductor(36, 75) == "8 AWG"
    assert select_conductor(1000, 75) is None


def test_select_conductor_90c():
    assert select_conductor(40, 90) == "10 AWG"
    assert select_conductor(536, 90) is None


def test_select_conductor_defaults_to_copper():
    assert select_conductor(35, 75) == select_conductor(35, 75, material="CU")


def test_select_conductor_aluminum_needs_larger_size_than_copper():
    # 4/0 AWG clears 230A at 75C for copper but not for aluminum (180A).
    assert select_conductor(201, 75, material="CU") == "4/0 AWG"
    assert select_conductor(201, 75, material="AL") == "250 kcmil"


def test_equipment_grounding_conductor_size_table_250_122():
    assert equipment_grounding_conductor_size(20, "CU") == "10 AWG"
    assert equipment_grounding_conductor_size(100, "CU") == "8 AWG"
    assert equipment_grounding_conductor_size(400, "CU") == "3 AWG"
    assert equipment_grounding_conductor_size(400, "AL") == "1 AWG"
    assert equipment_grounding_conductor_size(5000, "CU") == "3/0 AWG"  # clamps to table's top entry


def test_next_standard_size():
    assert next_standard_size(316.25) == 350
    assert next_standard_size(15) == 15
    assert next_standard_size(5000) == 1200


def test_grounding_conductor_size_breakpoints():
    assert grounding_conductor_size("10 AWG") == "8 AWG"
    assert grounding_conductor_size("2 AWG") == "8 AWG"
    assert grounding_conductor_size("1/0 AWG") == "6 AWG"
    assert grounding_conductor_size("750 kcmil") == "2/0 AWG"
