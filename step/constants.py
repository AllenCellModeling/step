#!/usr/bin/env python
# -*- coding: utf-8 -*-

###############################################################################


CONFIG_ENV_VAR_NAME = "WORKFLOW_CONFIG"
CWD_CONFIG_FILE_NAME = "workflow_config.json"

DEFAULT_QUILT_STORAGE = "s3://allencell-internal-quilt"
DEFAULT_PROJECT_LOCAL_STAGING_DIR = "{cwd}/local_staging"
DEFAULT_STEP_LOCAL_STAGING_DIR = "/".join(
    [DEFAULT_PROJECT_LOCAL_STAGING_DIR, "{module_name}"]
)
