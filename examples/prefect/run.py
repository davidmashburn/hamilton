# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import base64
import json
import tempfile
import uuid
from pathlib import Path
from typing import Any

import evaluate_model
import pandas as pd

# import modules containing your dataflow functions
import prepare_data
import train_model
from prefect import flow, get_run_logger, task
from prefect.artifacts import (
    create_markdown_artifact,
    create_progress_artifact,
    create_table_artifact,
    update_progress_artifact,
)

from hamilton import base, driver


def _publish_hamilton_execution_dag(
    dr: driver.Driver,
    final_vars: list[str],
    inputs: dict[str, Any],
    description: str,
) -> None:
    """Render a Hamilton execution DAG and attach it as a Prefect markdown artifact."""
    logger = get_run_logger()
    output_base = Path(tempfile.gettempdir()) / f"hamilton-dag-{uuid.uuid4().hex}"

    try:
        dr.visualize_execution(
            final_vars=final_vars,
            inputs=inputs,
            output_file_path=str(output_base),
            render_kwargs={"format": "png"},
        )

        image_path = output_base.with_suffix(".png")
        if not image_path.exists():
            raise FileNotFoundError(f"Expected DAG image not found at {image_path}")

        encoded_image = base64.b64encode(image_path.read_bytes()).decode("ascii")
        create_markdown_artifact(
            description=description,
            markdown=f"![{description}](data:image/png;base64,{encoded_image})",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Unable to publish Hamilton DAG artifact: %s", exc)
        create_markdown_artifact(
            description=f"{description} (generation failed)",
            markdown=f"Unable to render Hamilton DAG artifact: `{exc}`",
        )
    finally:
        for suffix in ("", ".png", ".dot", ".gv"):
            candidate = output_base.with_suffix(suffix) if suffix else output_base
            if candidate.exists():
                candidate.unlink(missing_ok=True)


# use the @task to define Prefect tasks, which adds logging, retries, etc.
# the function parameters define the config and inputs needed by Hamilton
@task
def prepare_data_task(
    raw_data_location: str,
    hamilton_config: dict,
    label: str,
    results_dir: str,
) -> str:
    """Load external data, preprocess dataset, and store cleaned data."""
    logger = get_run_logger()
    logger.info("Loading raw dataset from %s", raw_data_location)

    raw_df = pd.read_csv(raw_data_location, sep=";")

    dr = driver.Driver(hamilton_config, prepare_data)

    prepare_final_vars = prepare_data.ALL_FEATURES + [label]
    prepare_inputs = {"raw_df": raw_df}

    features_df = dr.execute(
        final_vars=prepare_final_vars,
        inputs=prepare_inputs,
    )

    _publish_hamilton_execution_dag(
        dr=dr,
        final_vars=prepare_final_vars,
        inputs=prepare_inputs,
        description="Hamilton DAG: prepare_data_task execution",
    )

    # Publish a quick preview so the Artifacts tab has something inspectable.
    create_table_artifact(
        description="Top 10 rows from engineered training data",
        table=features_df.head(10).reset_index().to_dict(orient="records"),
    )

    # save results to local file; for prod, save to an S3 bucket instead
    features_path = f"{results_dir}/features.csv"
    features_df.to_csv(features_path)

    create_markdown_artifact(
        description="Prepared feature dataset summary",
        markdown=(
            f"- Raw rows loaded: **{len(raw_df)}**\n"
            f"- Feature rows written: **{len(features_df)}**\n"
            f"- Feature columns: **{len(features_df.columns)}**\n"
            f"- Output path: `{features_path}`"
        ),
    )

    logger.info("Prepared %d rows into %s", len(features_df), features_path)
    return features_path


@task
def train_and_evaluate_model_task(
    features_path: str,
    hamilton_config: dict,
    label: str,
    feature_set: list[str],
    validation_user_ids: list[str],
) -> None:
    """Train and evaluate machine learning model."""
    logger = get_run_logger()

    progress_artifact_id = create_progress_artifact(
        progress=0.0,
        description="Model training/evaluation task progress",
    )

    update_progress_artifact(
        artifact_id=progress_artifact_id,
        progress=20.0,
        description="Initializing Hamilton driver",
    )

    dr = driver.Driver(
        hamilton_config,
        train_model,
        evaluate_model,
        adapter=base.SimplePythonGraphAdapter(base.DictResult()),
    )

    hamilton_final_vars = ["save_validation_preds", "model_results"]
    hamilton_inputs = dict(
        features_path=features_path,
        label=label,
        feature_set=feature_set,
        validation_user_ids=validation_user_ids,
    )

    logger.info("Executing train/evaluate pipeline with %d selected features", len(feature_set))
    update_progress_artifact(
        artifact_id=progress_artifact_id,
        progress=45.0,
        description="Running Hamilton model pipeline",
    )

    results = dr.execute(
        final_vars=hamilton_final_vars,
        inputs=hamilton_inputs,
    )

    _publish_hamilton_execution_dag(
        dr=dr,
        final_vars=hamilton_final_vars,
        inputs=hamilton_inputs,
        description="Hamilton DAG: train_and_evaluate_model_task execution",
    )

    update_progress_artifact(
        artifact_id=progress_artifact_id,
        progress=90.0,
        description="Model pipeline completed; publishing artifacts",
    )

    model_results = results.get("model_results")
    create_markdown_artifact(
        description="Model evaluation results",
        markdown=(
            "```json\n"
            f"{json.dumps(model_results, indent=2, default=str)}\n"
            "```"
        ),
    )

    save_validation_preds_output = results.get("save_validation_preds")
    create_markdown_artifact(
        description="Validation prediction save output",
        markdown=(
            "```json\n"
            f"{json.dumps(save_validation_preds_output, indent=2, default=str)}\n"
            "```"
        ),
    )

    update_progress_artifact(
        artifact_id=progress_artifact_id,
        progress=100.0,
        description="Training/evaluation task complete",
    )
    logger.info("Model training/evaluation task finished")


# use @flow to define the Prefect flow.
# the function parameters define the config and inputs needed by all tasks
# this way, we prevent having constants being hardcoded in the flow or task body
@flow(
    name="hamilton-absenteeism-prediction",
    description="Predict absenteeism using Hamilton and Prefect",
)
def absenteeism_prediction_flow(
    raw_data_location: str = "./data/Absenteeism_at_work.csv",
    feature_set: list[str] = [  # noqa: B006
        "age_zero_mean_unit_variance",
        "has_children",
        "has_pet",
        "is_summer",
        "service_time",
    ],
    label: str = "absenteeism_time_in_hours",
    validation_user_ids: list[str] = [  # noqa: B006
        "1",
        "2",
        "4",
        "15",
        "17",
        "24",
        "36",
    ],
):
    """Predict absenteeism using Hamilton and Prefect.

    The workflow is composed of 2 tasks, each with its own Hamilton driver.
    Notice that the task `prepare_data_task` relies on the Python module `prepare_data.py`,
    while the task `train_and_evaluate_model_task` relies on two Python modules
    `train_model.py` and `evaluate_model.py`.
    """

    # the task returns the string value `features_path`, by passing this value
    # to the next task, Prefect is able to generate the dependencies graph
    features_path = prepare_data_task(
        raw_data_location=raw_data_location,
        hamilton_config=dict(
            development_flag=True,
        ),
        label=label,
        results_dir="./data",
    )

    train_and_evaluate_model_task(
        features_path=features_path,
        hamilton_config=dict(
            development_flag=True,
            task="binary_classification",
            pred_path="./data/predictions.csv",
            model_config={},
            scorer_name="accuracy",
            bootstrap_iter=1000,
        ),
        label=label,
        feature_set=feature_set,
        validation_user_ids=validation_user_ids,
    )


if __name__ == "__main__":
    absenteeism_prediction_flow()
