"""resolve_module_spec: static catalog first, then a caller-supplied
ModuleSpec's own inline electricals for any SKU never added to this file's
small hardcoded set -- the common case now that the HMI's Module Spec panel
ships with no default parts list of its own."""

from __future__ import annotations

import pytest

from app.models import ModuleSpec, ProjectInput
from app.module_catalog import MODULE_SKUS, project_module_lookup, resolve_module_spec


def test_resolves_a_static_catalog_sku_even_with_no_fallback():
    spec = resolve_module_spec("720")
    assert spec.pmax == MODULE_SKUS["720"].pmax


def test_falls_back_to_the_supplied_modulespec_for_an_unknown_sku():
    fallback = ModuleSpec(sku="doc-1", pmax=500, voc=45, vmp=38, isc=14, imp=13.2, bifacial_pmax=550)
    spec = resolve_module_spec("doc-1", fallback)
    assert spec.pmax == 500
    assert spec.voc == 45
    assert spec.bifacial_pmax == 550


def test_fallback_is_ignored_when_its_sku_does_not_match():
    """A combiner row's own module_sku, not the project's main module -- the
    fallback should never be silently substituted for a different SKU."""
    fallback = ModuleSpec(sku="doc-1", pmax=500, voc=45, vmp=38, isc=14, imp=13.2)
    with pytest.raises(KeyError):
        resolve_module_spec("doc-2", fallback)


def test_fallback_with_no_real_data_still_raises():
    """Default-valued ModuleSpec (pmax/voc/isc all 0) means no data was
    actually supplied -- must not resolve to a bogus zeroed module."""
    fallback = ModuleSpec(sku="doc-1")
    with pytest.raises(KeyError):
        resolve_module_spec("doc-1", fallback)


def test_no_fallback_and_unknown_sku_raises():
    with pytest.raises(KeyError):
        resolve_module_spec("totally-unknown")


def test_dict_fallback_resolves_the_matching_entry_by_sku():
    """The HMI's Array schedule can put different modules on different
    inverters -- a project with more than one non-catalog module needs every
    one of them resolvable, not just the main project.module."""
    fallback = {
        "doc-1": ModuleSpec(sku="doc-1", pmax=500, voc=45, vmp=38, isc=14, imp=13.2),
        "doc-2": ModuleSpec(sku="doc-2", pmax=600, voc=48, vmp=40, isc=15, imp=14.1),
    }
    spec1 = resolve_module_spec("doc-1", fallback)
    spec2 = resolve_module_spec("doc-2", fallback)
    assert spec1.pmax == 500
    assert spec2.pmax == 600


def test_dict_fallback_raises_for_a_sku_not_in_the_dict():
    fallback = {"doc-1": ModuleSpec(sku="doc-1", pmax=500, voc=45, vmp=38, isc=14, imp=13.2)}
    with pytest.raises(KeyError):
        resolve_module_spec("doc-2", fallback)


def test_project_module_lookup_includes_main_module_and_custom_modules():
    project = ProjectInput(
        module=ModuleSpec(sku="main-1", pmax=700, voc=49, vmp=41, isc=18, imp=17),
        custom_modules={
            "doc-1": ModuleSpec(sku="doc-1", pmax=500, voc=45, vmp=38, isc=14, imp=13.2),
        },
    )
    lookup = project_module_lookup(project)
    assert set(lookup) == {"main-1", "doc-1"}
    assert resolve_module_spec("main-1", lookup).pmax == 700
    assert resolve_module_spec("doc-1", lookup).pmax == 500


def test_project_module_lookup_main_module_is_not_shadowed_by_custom_modules():
    """The main module is applied last -- a custom_modules entry that happens
    to share its sku can't accidentally replace it."""
    main = ModuleSpec(sku="main-1", pmax=700, voc=49, vmp=41, isc=18, imp=17)
    project = ProjectInput(
        module=main,
        custom_modules={"main-1": ModuleSpec(sku="main-1", pmax=999, voc=1, vmp=1, isc=1, imp=1)},
    )
    lookup = project_module_lookup(project)
    assert lookup["main-1"].pmax == 700
