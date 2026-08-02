"""Client for the private ditto-data-pipeline generate service.

The platform draws a fresh seed per submission at job-ready, calls the generate
service to render the dataset, and pins (seed, dataset_sha256, run_size) on the
agent so all k=3 validators score the identical dataset.
"""

from ditto.api_server.datapipeline.client import (
    DatasetGenerator,
    HttpDatasetGenerator,
    NullGenerator,
    create_generator,
)
from ditto.api_server.datapipeline.config import (
    DataPipelineConfig,
    check_data_pipeline_config,
    parse_data_pipeline_config_from_env,
)
from ditto.api_server.datapipeline.errors import (
    DataPipelineConfigError,
    DataPipelineError,
)

__all__ = [
    "DataPipelineConfig",
    "DataPipelineConfigError",
    "DataPipelineError",
    "DatasetGenerator",
    "HttpDatasetGenerator",
    "NullGenerator",
    "check_data_pipeline_config",
    "create_generator",
    "parse_data_pipeline_config_from_env",
]
