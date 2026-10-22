"""
Training-data ingestion: package inspection, adapters, normalization.

The path every training upload takes, in order:

    package_inspector  -> safe files (ZIPs expanded under limits)
    adapter_registry   -> which adapters match, with evidence
    adapter.normalize  -> canonical store_day_v1 frame + retained context
    training.contract  -> strict canonical validation and readiness

Detection never implies acceptance: a recognised source still goes through
the same strict validator as a canonical upload.
"""
