"""Packaged JSON Schemas used at runtime.

The repository-level ``schemas/`` directory remains the reviewable contract
copy.  The files under this package are shipped in wheels so an installed
package can validate experiment documents without depending on the checkout
layout.  ``tests/test_packaging.py`` keeps both copies byte-identical.
"""
