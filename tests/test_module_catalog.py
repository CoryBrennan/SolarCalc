"""resolve_module_spec: static catalog first, then a caller-supplied
ModuleSpec's own inline electricals for any SKU never added to this file's
small hardcoded set -- the common case now that the HMI's Module Spec panel
ships with no default parts list of its own."""

from __future__ import annotations

import pytest

from app.models import ModuleSpec
from app.module_catalog import MODULE_SKUS, resolve_module_spec


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
