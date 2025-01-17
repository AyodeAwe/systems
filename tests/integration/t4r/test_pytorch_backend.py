#
# Copyright (c) 2022, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import pytest

np = pytest.importorskip("numpy")
torch = pytest.importorskip("torch")
t4r = pytest.importorskip("transformers4rec")
tr = pytest.importorskip("transformers4rec.torch")

triton = pytest.importorskip("merlin.systems.triton")
data_conversions = pytest.importorskip("merlin.systems.triton.conversions")

tritonclient = pytest.importorskip("tritonclient")
grpcclient = pytest.importorskip("tritonclient.grpc")

from merlin.core.dispatch import make_df  # noqa
from merlin.systems.dag import Ensemble  # noqa
from merlin.systems.dag.ops.pytorch import PredictPyTorch  # noqa
from merlin.systems.triton.utils import run_ensemble_on_tritonserver  # noqa


def test_serve_t4r_with_torchscript(tmpdir):

    # ===========================================
    # Generate training data
    # ===========================================

    min_session_len = 5
    max_session_len = 20
    torch_yoochoose_like = tr.data.tabular_sequence_testing_data.torch_synthetic_data(
        num_rows=100,
        min_session_length=min_session_len,
        max_session_length=max_session_len,
        device="cuda",
    )
    t4r_yoochoose_schema = t4r.data.tabular_sequence_testing_data.schema

    # ===========================================
    # Build, train, test, and JIT the model
    # ===========================================

    input_module = t4r.torch.TabularSequenceFeatures.from_schema(
        t4r_yoochoose_schema,
        max_sequence_length=20,
        d_output=64,
        masking="causal",
    )
    prediction_task = t4r.torch.NextItemPredictionTask(weight_tying=True)
    transformer_config = t4r.config.transformer.XLNetConfig.build(
        d_model=64, n_head=8, n_layer=2, total_seq_length=20
    )
    model = transformer_config.to_torch_model(input_module, prediction_task)
    model = model.cuda()

    _ = model(torch_yoochoose_like)

    model.eval()

    traced_model = torch.jit.trace(model, torch_yoochoose_like, strict=True)
    assert isinstance(traced_model, torch.jit.TopLevelTracedModule)
    assert torch.allclose(
        model(torch_yoochoose_like),
        traced_model(torch_yoochoose_like),
    )

    # ===========================================
    # Build a simple Ensemble graph
    # ===========================================

    input_schema = model.input_schema
    output_schema = model.output_schema

    torch_op = input_schema.column_names >> PredictPyTorch(
        traced_model, input_schema, output_schema
    )

    ensemble = Ensemble(torch_op, input_schema)
    ens_config, node_configs = ensemble.export(str(tmpdir))

    # ===========================================
    # Create Request Data
    # ===========================================

    request_data = tr.data.tabular_sequence_testing_data.torch_synthetic_data(
        num_rows=40,
        min_session_length=4,
        max_session_length=10,
        device="cuda",
    )

    df_cols = {}
    for name, tensor in request_data.items():
        if name in input_schema.column_names:
            dtype = input_schema[name].dtype

            df_cols[name] = tensor.cpu().numpy().astype(dtype)
            if len(tensor.shape) > 1:
                df_cols[name] = list(df_cols[name])

    df = make_df(df_cols)

    # ===========================================
    # Send request to Triton and check response
    # ===========================================
    triton_response = run_ensemble_on_tritonserver(
        tmpdir, input_schema, df, output_schema.column_names, "executor_model"
    )

    assert triton_response

    preds_triton = triton_response[output_schema.column_names[0]]

    preds_model = model(request_data).cpu().detach().numpy()

    np.testing.assert_allclose(preds_triton, preds_model)
