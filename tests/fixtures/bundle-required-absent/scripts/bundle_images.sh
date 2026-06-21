#!/usr/bin/env bash
# Fixture: sources the shared bundle-lib.sh but does NOT vendor it.
# validate_bundle_lib.py must FAIL on this (missing-but-sourced).
. scripts/bundle-lib.sh
