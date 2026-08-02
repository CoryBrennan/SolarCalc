from app.conductor_tables import grounding_conductor_size, next_standard_size, select_conductor


def test_select_conductor_75c():
    assert select_conductor(35, 75) == "10 AWG"
    assert select_conductor(36, 75) == "8 AWG"
    assert select_conductor(1000, 75) is None


def test_select_conductor_90c():
    assert select_conductor(40, 90) == "10 AWG"
    assert select_conductor(536, 90) is None


def test_next_standard_size():
    assert next_standard_size(316.25) == 350
    assert next_standard_size(15) == 15
    assert next_standard_size(5000) == 1200


def test_grounding_conductor_size_breakpoints():
    assert grounding_conductor_size("10 AWG") == "8 AWG"
    assert grounding_conductor_size("2 AWG") == "8 AWG"
    assert grounding_conductor_size("1/0 AWG") == "6 AWG"
    assert grounding_conductor_size("750 kcmil") == "2/0 AWG"
