

from abc import abstractmethod
from typing import List, Optional

import torch
from torch.nn.parameter import Parameter
import torch.nn.functional as F
from vllm.distributed import (divide, get_tensor_model_parallel_rank,
                              get_tensor_model_parallel_world_size,
                              split_tensor_along_last_dim,
                              tensor_model_parallel_all_gather,
                              tensor_model_parallel_all_reduce)
from vllm.logger import init_logger
from vllm.model_executor.layers.quantization.base_config import (
    QuantizationConfig, QuantizeMethodBase)
#from vllm.model_executor.layers.tuned_gemm import tgemm
from vllm.model_executor.utils import set_weight_attrs

logger = init_logger(__name__)


def adjust_marlin_shard(param, shard_size, shard_offset):
    marlin_tile_size = getattr(param, "marlin_tile_size", None)
    if marlin_tile_size is None:
        return shard_size, shard_offset

    return shard_size * marlin_tile_size, shard_offset * marlin_tile_size


class LinearMethodBase(QuantizeMethodBase):
    """Base class for different (maybe quantized) linear methods."""

    @abstractmethod
    def create_weights(self, layer: torch.nn.Module,
                       input_size_per_partition: int,
                       output_partition_sizes: List[int], input_size: int,
                       output_size: int, params_dtype: torch.dtype,
                       **extra_weight_attrs):
        """Create weights for a linear layer.
           The weights will be set as attributes of the layer.

        Args:
            layer: The layer that is using the LinearMethodBase factory.
            input_size_per_partition: Size of the weight input dim on rank X.
            output_partition_sizes: Sizes of the output dim of each logical
                weight on rank X. E.g., output_partition_sizes for QKVLinear
                is a list contains the width of Wq, Wk, Wv on rank X.
            input_size: Size of the input dim of the weight across all ranks.
            output_size: Size of the output dim of the weight across all ranks.
            params_dtype: Datatype of the parameters.
        """
        raise NotImplementedError

    @abstractmethod
    def apply(self,
              layer: torch.nn.Module,
              x: torch.Tensor,
              bias: Optional[torch.Tensor] = None) -> torch.Tensor:
        """Apply the weights in layer to the input tensor.
        Expects create_weights to have been called before on the layer."""
        raise NotImplementedError


class UnquantizedLinearMethod(LinearMethodBase):
    """Linear method without quantization.

    Args:
        separate_bias_add: If true, add bias separately after matrix
                           multiplication.
    """

    def __init__(self, separate_bias_add: bool = False):
        self.separate_bias_add = separate_bias_add

    def create_weights(self, layer: torch.nn.Module,
                       input_size_per_partition: int,
                       output_partition_sizes: List[int], input_size: int,
                       output_size: int, params_dtype: torch.dtype,
                       **extra_weight_attrs):
        weight = Parameter(torch.empty(sum(output_partition_sizes),
                                       input_size_per_partition,
                                       dtype=params_dtype),
                           requires_grad=False)
        set_weight_attrs(weight, {"input_dim": 1, "output_dim": 0})
        layer.register_parameter("weight", weight)
        set_weight_attrs(weight, extra_weight_attrs)

    def apply(self,
              layer: torch.nn.Module,
              x: torch.Tensor,
              bias: Optional[torch.Tensor] = None) -> torch.Tensor:
        weight = layer.weight
#        if self.separate_bias_add and bias is not None:
#            return tgemm.mm(x, weight) + bias
#        return tgemm.mm(x, weight, bias)
        return F.linear(x, weight, bias)


class LinearBase(torch.nn.Module):
    """Base linear layer.

    Args:
        input_size: input dimension of the linear layer.
        output_size: output dimension of the linear layer.
        bias: If true, add bias.
        skip_bias_add: If true, skip adding bias but instead return it.
        params_dtype: Data type for the parameters.
        quant_config: Quantization configure.
    """

    def __init__(
        self,
        input_size: int,
        output_size: int,
        skip_bias_add: bool = False,
        params_dtype: Optional[torch.dtype] = None,
        quant_config: Optional[QuantizationConfig] = None,
    ):
        super().__init__()

        # Keep input parameters
        self.input_size = input_size
        self.output_size = output_size
        self.skip_bias_add = skip_bias_add
        if params_dtype is None:
            params_dtype = torch.get_default_dtype()
        self.params_dtype = params_dtype
        if quant_config is None:
            self.quant_method: Optional[
                QuantizeMethodBase] = UnquantizedLinearMethod()
        else:
            self.quant_method = quant_config.get_quant_method(self)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        raise NotImplementedError


class ReplicatedLinear(LinearBase):
    """Replicated linear layer.

    Args:
        input_size: input dimension of the linear layer.
        output_size: output dimension of the linear layer.
        bias: If true, add bias.
        skip_bias_add: If true, skip adding bias but instead return it.
        params_dtype: Data type for the parameters.
        quant_config: Quantization configure.
    """

    def __init__(self,
                 input_size: int,
                 output_size: int,
                 bias: bool = True,
                 skip_bias_add: bool = False,
                 params_dtype: Optional[torch.dtype] = None,
                 quant_config: Optional[QuantizationConfig] = None):
        super().__init__(input_size, output_size, skip_bias_add, params_dtype,
                         quant_config)

        # All the linear layer supports quant method.
        assert self.quant_method is not None
        self.quant_method.create_weights(self, self.input_size,
                                         [self.output_size], self.input_size,
                                         self.output_size, self.params_dtype)

        if bias:
            self.bias = Parameter(
                torch.empty(self.output_size, dtype=self.params_dtype))
            set_weight_attrs(self.bias, {"output_dim": 0})
        else:
            self.register_parameter("bias", None)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        bias = self.bias if not self.skip_bias_add else None
        assert self.quant_method is not None
        output = self.quant_method.apply(self, x, bias)
        output_bias = self.bias if self.skip_bias_add else None
        return output, output_bias

    def extra_repr(self) -> str:
        s = f"in_features={self.input_size}"
        s += f", output_features={self.output_size}"
        s += f", bias={self.bias is not None}"
        return s


class ColumnParallelLinear(LinearBase):
    """Linear layer with column parallelism.

    The linear layer is defined as Y = XA + b. A is parallelized along
    its second dimension as A = [A_1, ..., A_p].

    Args:
        input_size: first dimension of matrix A.
        output_size: second dimension of matrix A.
        bias: If true, add bias.
        gather_output: If true, call all-gather on output and make Y available
                       to all GPUs, otherwise, every GPU will have its output
                       which is Y_i = XA_i
        skip_bias_add: This was added to enable performance optimizations where
                       bias can be fused with other element-wise operations. we
                       skip adding bias but instead return it.
        params_dtype: Data type for the parameters.
        quant_config: Quantization configure.
        output_sizes: list of output sizes packed into one output, like for QKV
                       the list would be size 3.
    """

    def __init__(self,
                 input_size: int,
                 output_size: int,
                 bias: bool = True,
                 gather_output: bool = False,
                 skip_bias_add: bool = False,
                 params_dtype: Optional[torch.dtype] = None,
                 quant_config: Optional[QuantizationConfig] = None,
                 output_sizes: Optional[List[int]] = None):
        super().__init__(input_size, output_size, skip_bias_add, params_dtype,
                         quant_config)

        self.gather_output = gather_output

        # Divide the weight matrix along the last dimension.
        tp_size = get_tensor_model_parallel_world_size()
        assert self.quant_method is not None
        self.output_size_per_partition = divide(self.output_size, tp_size)
        self.output_partition_sizes = [self.output_size_per_partition]
        # If QKV or MergedColumn, use output size of each partition.
        if hasattr(self, "output_sizes"):
            self.output_partition_sizes = [
                divide(output_size, tp_size)
                for output_size in self.output_sizes
            ]

        if output_sizes is None:
            output_sizes = [output_size]
        self.quant_method.create_weights(
            layer=self,
            input_size_per_partition=self.input_size,
            output_partition_sizes=self.output_partition_sizes,
            input_size=self.input_size,
            output_size=self.output_size,
            params_dtype=self.params_dtype,
            weight_loader=self.weight_loader)
        if bias:
            self.bias = Parameter(
                torch.empty(self.output_size_per_partition,
                            dtype=params_dtype))
            set_weight_attrs(self.bias, {
                "output_dim": 0,
                "weight_loader": self.weight_loader,
            })
        else:
            self.register_parameter("bias", None)

    def weight_loader(self, param: Parameter, loaded_weight: torch.Tensor):
        # Special case for Fp8 scales.
        fp8_scales_shard_indexer = getattr(param, "fp8_scales_shard_indexer",
                                           None)

        tp_rank = get_tensor_model_parallel_rank()
        output_dim = getattr(param, "output_dim", None)
        param_data = param.data
        if output_dim is not None:
            shard_size = param_data.shape[output_dim]
            start_idx = tp_rank * shard_size
            loaded_weight = loaded_weight.narrow(output_dim, start_idx,
                                                 shard_size)
        # Special case for Fp8 scales.
        elif fp8_scales_shard_indexer is not None:
            param_data, loaded_weight = fp8_scales_shard_indexer(param_data,
                                                                 loaded_weight,
                                                                 shard_id=0)

        assert param_data.shape == loaded_weight.shape
        param_data.copy_(loaded_weight)

    def forward(self, input_):
        bias = self.bias if not self.skip_bias_add else None

        # Matrix multiply.
        assert self.quant_method is not None
        output_parallel = self.quant_method.apply(self, input_, bias)
        if self.gather_output:
            # All-gather across the partitions.
            output = tensor_model_parallel_all_gather(output_parallel)
        else:
            output = output_parallel
        output_bias = self.bias if self.skip_bias_add else None
        return output, output_bias

    def extra_repr(self) -> str:
        s = f"in_features={self.input_size}"
        s += f", output_features={self.output_size_per_partition}"
        s += f", bias={self.bias is not None}"
        s += f", tp_size={get_tensor_model_parallel_world_size()}"
        s += f", gather_output={self.gather_output}"
        return s


class MergedColumnParallelLinear(ColumnParallelLinear):
    """Packed linear layers with column parallelism.

    Similar to ColumnParallelLinear, but the weight matrix is concatenated
    along the output dimension. When the weight matrix is loaded, the
    different partitions are sharded separately.

    Args:
        input_size: input dimension of the linear layer.
        output_sizes: list of output dimensions of the linear layer.
        bias: If true, add bias.
        gather_output: If true, call all-gather on output and make the output
                       available to all GPUs, otherwise, every GPU will have
                       its own output.
        skip_bias_add: This was added to enable performance optimizations where
                       bias can be fused with other element-wise operations. we
                       skip adding bias but instead return it.
        params_dtype: Data type for the parameters.
        quant_config: Quantization configure.
    """

    def __init__(self,
                 input_size: int,
                 output_sizes: List[int],
                 bias: bool = True,
                 gather_output: bool = False,
                 skip_bias_add: bool = False,
                 params_dtype: Optional[torch.dtype] = None,
                 quant_config: Optional[QuantizationConfig] = None):
        self.output_sizes = output_sizes
        tp_size = get_tensor_model_parallel_world_size()
        assert all(output_size % tp_size == 0 for output_size in output_sizes)
        super().__init__(input_size=input_size,
                         output_size=sum(output_sizes),
                         bias=bias,
                         gather_output=gather_output,
                         skip_bias_add=skip_bias_add,
                         params_dtype=params_dtype,
                         quant_config=quant_config)

    def weight_loader(self,
                      param: Parameter,
                      loaded_weight: torch.Tensor,
                      loaded_shard_id: Optional[int] = None):

        param_data = param.data
        output_dim = getattr(param, "output_dim", None)
        # Special case for AQLM codebooks.
        is_metadata = getattr(param, "is_metadata", False)

        param_shard_splitter = getattr(param, "shard_splitter", None)

        if output_dim is not None and param_shard_splitter is not None:
            raise NotImplementedError(
                "We do not currently support output_dim != None and "
                "shard_splitter != None for a parameter. Please open an issue."
            )
        # If a parameter has defined a shard_splitter to be used for
        # the weight, it should be applied before the weight is
        # loaded/copied to the parameter. The shard_splitter applies
        # logic by using the loaded_shard_id to ensure that the loaded
        # param is loaded to the correct location
        # within the parameter defined by the linear method.
        if loaded_shard_id is None and param_shard_splitter is not None:
            raise NotImplementedError(
                "We do not currently support loaded_shard_id == None and "
                "shard_splitter != None for a parameter. Please open an issue."
            )

        # Special case for Fp8 scales.
        fp8_scales_shard_indexer = getattr(param, "fp8_scales_shard_indexer",
                                           None)

        if loaded_shard_id is None:
            # Loaded weight is already packed.
            if output_dim is None:
                assert param_data.shape == loaded_weight.shape
                param_data.copy_(loaded_weight)
                return
            current_shard_offset = 0
            shard_offsets = []
            for i, output_size in enumerate(self.output_sizes):
                shard_offsets.append((i, current_shard_offset, output_size))
                current_shard_offset += output_size
            packed_dim = getattr(param, "packed_dim", None)
            for shard_id, shard_offset, shard_size in shard_offsets:
                # Special case for Quantization.
                # If quantized, we need to adjust the offset and size to account
                # for the packing.
                if packed_dim == output_dim:
                    shard_size = shard_size // param.pack_factor
                    shard_offset = shard_offset // param.pack_factor
                    # Special case for Marlin.
                    shard_size, shard_offset = adjust_marlin_shard(
                        param, shard_size, shard_offset)

                loaded_weight_shard = loaded_weight.narrow(
                    output_dim, shard_offset, shard_size)
                self.weight_loader(param, loaded_weight_shard, shard_id)
            return

        assert loaded_shard_id < len(self.output_sizes)
        tp_rank = get_tensor_model_parallel_rank()
        tp_size = get_tensor_model_parallel_world_size()
        if output_dim is not None:
            shard_offset = sum(self.output_sizes[:loaded_shard_id]) // tp_size
            shard_size = self.output_sizes[loaded_shard_id] // tp_size
            # Special case for quantization.
            # If quantized, we need to adjust the offset and size to account
            # for the packing.
            packed_dim = getattr(param, "packed_dim", None)
            if packed_dim == output_dim:
                shard_size = shard_size // param.pack_factor
                shard_offset = shard_offset // param.pack_factor
                # Special case for Marlin.
                shard_size, shard_offset = adjust_marlin_shard(
                    param, shard_size, shard_offset)

            param_data = param_data.narrow(output_dim, shard_offset,
                                           shard_size)
            start_idx = tp_rank * shard_size
            loaded_weight = loaded_weight.narrow(output_dim, start_idx,
                                                 shard_size)
        # Special case for AQLM codebooks.
        elif is_metadata:
            # metadata indicates fixed size concatenated along dim 0
            shard_size = loaded_weight.shape[0]
            shard_offset = loaded_shard_id * shard_size
            param_data = param_data.narrow(0, shard_offset, shard_size)

        # If a param_shard_splitter is defined by the LinearMethod, use it.
        elif param_shard_splitter is not None:
            logical_widths = getattr(param, "logical_widths", None)
            param_data, loaded_weight = param_shard_splitter(
                param_data, loaded_weight, loaded_shard_id, logical_widths)

        # Special case for Fp8 scales.
        elif fp8_scales_shard_indexer is not None:
            param_data, loaded_weight = fp8_scales_shard_indexer(
                param_data, loaded_weight, loaded_shard_id)

        else:
            ignore_warning = getattr(param, "ignore_warning", False)
            if not ignore_warning:
                logger.warning(
                    "Loading a weight without `output_dim` attribute in "
                    "MergedColumnParallelLinear, assume the weight is "
                    "the same for all partitions.")

        if fp8_scales_shard_indexer is None:
            if len(param_data.shape) == 0:
                param_data = param_data.reshape(1)

            if len(loaded_weight.shape) == 0:
                loaded_weight = loaded_weight.reshape(1)

        assert param_data.shape == loaded_weight.shape
        param_data.copy_(loaded_weight)


class QKVParallelLinear(ColumnParallelLinear):
    """Linear layers for the attention's QKV transformation.

    Linear layers for the linear transformation of the query, key, and value
    vectors in the attention layer. The weight matrix is concatenated along
    the output dimension. The layer is parallelized along the head dimension.
    When the number of key/value heads is smaller than the number of query
    heads (e.g., multi-query/grouped-query attention), the key/value head may
    be replicated while the query heads are partitioned.

    Args:
        hidden_size: input hidden state size of the transformer.
        head_size: size of each attention head.
        total_num_heads: total number of attention query heads.
        total_num_kv_heads: total number of attention key/value heads. If
                            None, assume total_num_kv_heads = total_num_heads.
        bias: If true, add bias.
        skip_bias_add: This was added to enable performance optimizations where
                       bias can be fused with other element-wise operations. we
                       skip adding bias but instead return it.
        params_dtype: Data type for the parameters.
        quant_config: Quantization configure.
    """

    def __init__(self,
                 hidden_size: int,
                 head_size: int,
                 total_num_heads: int,
                 total_num_kv_heads: Optional[int] = None,
                 bias: bool = True,
                 skip_bias_add: bool = False,
                 params_dtype: Optional[torch.dtype] = None,
                 quant_config: Optional[QuantizationConfig] = None):
        self.hidden_size = hidden_size
        self.head_size = head_size
        self.total_num_heads = total_num_heads
        if total_num_kv_heads is None:
            total_num_kv_heads = total_num_heads
        self.total_num_kv_heads = total_num_kv_heads
        # Divide the weight matrix along the last dimension.
        tp_size = get_tensor_model_parallel_world_size()
        self.num_heads = divide(self.total_num_heads, tp_size)
        if tp_size >= self.total_num_kv_heads:
            self.num_kv_heads = 1
            self.num_kv_head_replicas = divide(tp_size,
                                               self.total_num_kv_heads)
        else:
            self.num_kv_heads = divide(self.total_num_kv_heads, tp_size)
            self.num_kv_head_replicas = 1
        input_size = self.hidden_size
        output_size = (self.num_heads +
                       2 * self.num_kv_heads) * tp_size * self.head_size
        self.output_sizes = [
            self.num_heads * self.head_size * tp_size,  # q_proj
            self.num_kv_heads * self.head_size * tp_size,  # k_proj
            self.num_kv_heads * self.head_size * tp_size,  # v_proj
        ]

        super().__init__(input_size=input_size,
                         output_size=output_size,
                         bias=bias,
                         gather_output=False,
                         skip_bias_add=skip_bias_add,
                         params_dtype=params_dtype,
                         quant_config=quant_config)

    def weight_loader(self,
                      param: Parameter,
                      loaded_weight: torch.Tensor,
                      loaded_shard_id: Optional[str] = None):
        param_data = param.data
        print("********")
        output_dim = getattr(param, "output_dim", None)
        # Special case for AQLM codebooks.
        is_metadata = getattr(param, "is_metadata", False)

        param_shard_splitter = getattr(param, "shard_splitter", None)

        if output_dim is not None and param_shard_splitter is not None:
            raise NotImplementedError(
                "We do not currently support output_dim != None and "
                "shard_splitter != None for a parameter. Please open an issue."
            )
        # If a parameter has defined a shard_splitter to be used for
        # the weight, it should be applied before the weight is
        # loaded/copied to the parameter. The shard_splitter applies
        # logic by using the loaded_shard_id to ensure that the loaded
        # param is loaded to the correct location
        # within the parameter defined by the linear method.
        if loaded_shard_id is None and param_shard_splitter is not None:
            raise NotImplementedError(
                "We do not currently support loaded_shard_id == None and "
                "shard_splitter != None for a parameter. Please open an issue."
            )

        # Special case for Fp8 scales.
        fp8_scales_shard_indexer = getattr(param, "fp8_scales_shard_indexer",
                                           None)

        if loaded_shard_id is None:
            # Loaded weight is already packed.
            if output_dim is None:
                assert param_data.shape == loaded_weight.shape
                param_data.copy_(loaded_weight)
                return
            shard_offsets = [
                # (shard_id, shard_offset, shard_size)
                ("q", 0, self.total_num_heads * self.head_size),
                ("k", self.total_num_heads * self.head_size,
                 self.total_num_kv_heads * self.head_size),
                ("v", (self.total_num_heads + self.total_num_kv_heads) *
                 self.head_size, self.total_num_kv_heads * self.head_size),
            ]
            packed_dim = getattr(param, "packed_dim", None)
            for shard_id, shard_offset, shard_size in shard_offsets:
                # Special case for Quantized Weights.
                # If quantized, we need to adjust the offset and size to account
                # for the packing.
                if packed_dim == output_dim:
                    shard_size = shard_size // param.pack_factor
                    shard_offset = shard_offset // param.pack_factor

                    # Special case for Marlin.
                    shard_size, shard_offset = adjust_marlin_shard(
                        param, shard_size, shard_offset)

                loaded_weight_shard = loaded_weight.narrow(
                    output_dim, shard_offset, shard_size)
                self.weight_loader(param, loaded_weight_shard, shard_id)
            return

        tp_rank = get_tensor_model_parallel_rank()
        assert loaded_shard_id in ["q", "k", "v"]

        # If output dim is defined, use the default loading process.
        if output_dim is not None:
            if loaded_shard_id == "q":
                shard_offset = 0
                shard_size = self.num_heads * self.head_size
            elif loaded_shard_id == "k":
                shard_offset = self.num_heads * self.head_size
                shard_size = self.num_kv_heads * self.head_size
            elif loaded_shard_id == "v":
                shard_offset = (self.num_heads +
                                self.num_kv_heads) * self.head_size
                shard_size = self.num_kv_heads * self.head_size
            # Special case for Quantized Weights.
            # If quantized, we need to adjust the offset and size to account
            # for the packing.
            packed_dim = getattr(param, "packed_dim", None)
            if packed_dim == output_dim:
                shard_size = shard_size // param.pack_factor
                shard_offset = shard_offset // param.pack_factor

                # Special case for Marlin.
                shard_size, shard_offset = adjust_marlin_shard(
                    param, shard_size, shard_offset)
            print("&&&&&&")
            print(loaded_shard_id, param_data.shape, loaded_weight.shape)
            print("&&&&&&")
            param_data = param_data.narrow(output_dim, shard_offset,
                                           shard_size)
            if loaded_shard_id == "q":
                shard_id = tp_rank
            else:
                shard_id = tp_rank // self.num_kv_head_replicas
            start_idx = shard_id * shard_size
            loaded_weight = loaded_weight.narrow(output_dim, start_idx,
                                                 shard_size)
        # Special case for for AQLM codebooks.
        elif is_metadata:
            # metadata indicates fixed size concatenated along dim 0
            shard_size = loaded_weight.shape[0]
            shard_index = ["q", "k", "v"].index(loaded_shard_id)
            param_data = param_data.narrow(0, shard_index * shard_size,
                                           shard_size)
        # If a param_shard_splitter is defined by the LinearMethod, use it.
        elif param_shard_splitter is not None:
            logical_widths = getattr(param, "logical_widths", None)
            param_data, loaded_weight = param_shard_splitter(
                param_data, loaded_weight, loaded_shard_id, logical_widths)

        # Special case for Fp8 scales.
        elif fp8_scales_shard_indexer is not None:
            param_data, loaded_weight = fp8_scales_shard_indexer(
                param_data, loaded_weight, loaded_shard_id)
        else:
            ignore_warning = getattr(param, "ignore_warning", False)
            if not ignore_warning:
                logger.warning(
                    "Loading a weight without `output_dim` attribute in "
                    "QKVParallelLinear, assume the weight is the same "
                    "for all partitions.")

        if len(param_data.shape) == 0:
            param_data = param_data.reshape(1)

        if len(loaded_weight.shape) == 0:
            loaded_weight = loaded_weight.reshape(1)

        print(loaded_shard_id, param_data.shape, loaded_weight.shape)
        print("********")
        assert param_data.shape == loaded_weight.shape
        param_data.copy_(loaded_weight)

    def forward(self, input_):
        bias = self.bias if not self.skip_bias_add else None

        # Matrix multiply.
        assert self.quant_method is not None
        output_parallel = self.quant_method.apply(self, input_, bias)
        q_sub, k_sub, v_sub = output_parallel.split([self.num_heads * self.head_size, self.num_kv_heads * self.head_size, self.num_kv_heads * self.head_size], dim=-1)
        tp_rank = get_tensor_model_parallel_rank()
        if (tp_rank == 0):
            print(k_sub)
        if self.gather_output:
            # All-gather across the partitions.
            output = tensor_model_parallel_all_gather(output_parallel)
        else:
            output = output_parallel
        output_bias = self.bias if self.skip_bias_add else None
        return output, output_bias

class QKVParallelLinearModified(ColumnParallelLinear):
    """Linear layers for the attention's QKV transformation.

    Linear layers for the linear transformation of the query, key, and value
    vectors in the attention layer. The weight matrix is concatenated along
    the output dimension. The layer is parallelized along the head dimension.
    When the number of key/value heads is smaller than the number of query
    heads (e.g., multi-query/grouped-query attention), the key/value head may
    be replicated while the query heads are partitioned.

    Args:
        hidden_size: input hidden state size of the transformer.
        head_size: size of each attention head.
        total_num_heads: total number of attention query heads.
        total_num_kv_heads: total number of attention key/value heads. If
                            None, assume total_num_kv_heads = total_num_heads.
        bias: If true, add bias.
        skip_bias_add: This was added to enable performance optimizations where
                       bias can be fused with other element-wise operations. we
                       skip adding bias but instead return it.
        params_dtype: Data type for the parameters.
        quant_config: Quantization configure.
    """

    def __init__(self,
                 hidden_size: int,
                 head_size: int,
                 total_num_heads: int,
                 total_num_kv_heads: Optional[int] = None,
                 bias: bool = True,
                 skip_bias_add: bool = False,
                 params_dtype: Optional[torch.dtype] = None,
                 quant_config: Optional[QuantizationConfig] = None):
        self.hidden_size = hidden_size
        self.head_size = head_size
        self.total_num_heads = total_num_heads
        if total_num_kv_heads is None:
            total_num_kv_heads = total_num_heads
        self.total_num_kv_heads = total_num_kv_heads
        # Divide the weight matrix along the last dimension.
        tp_size = get_tensor_model_parallel_world_size()
        self.num_heads = divide(self.total_num_heads, tp_size)
        if tp_size >= self.total_num_kv_heads:
            self.num_kv_heads = 1
            self.num_kv_head_replicas = divide(tp_size,
                                               self.total_num_kv_heads)
        else:
            self.num_kv_heads = divide(self.total_num_kv_heads, tp_size)
            self.num_kv_head_replicas = 1
        input_size = self.hidden_size
        output_size = (self.num_heads +
                       2 * self.num_kv_heads) * tp_size * self.head_size

        self.output_sizes = []
        self.output_sizes.append(self.num_heads * self.head_size * tp_size)  #q_proj
        for i in range(self.total_num_kv_heads):                               #k_proj
            self.output_sizes.append(self.head_size)    # NO tp_size multiply because we split it across each matrix
        for i in range(self.total_num_kv_heads):                               #v_proj
            self.output_sizes.append(self.head_size)    # NO tp_size multiply because we split it across each matrix
        #self.output_sizes.append(self.num_kv_heads * self.head_size * tp_size)  #v_proj
        #self.output_sizes = [
        #    self.num_heads * self.head_size * tp_size,  # q_proj
        #    self.num_kv_heads * self.head_size * tp_size,  # k_proj
        #    self.num_kv_heads * self.head_size * tp_size,  # v_proj
        #]

        super().__init__(input_size=input_size,
                         output_size=sum(self.output_sizes),
                         bias=bias,
                         gather_output=False,
                         skip_bias_add=skip_bias_add,
                         params_dtype=params_dtype,
                         quant_config=quant_config)

    def weight_loader(self,
                      param: Parameter,
                      loaded_weight: torch.Tensor,
                      loaded_shard_id: Optional[str] = None):
        param_data = param.data
        param_unmodified_data = param.data
        loaded_unmodified_weight = loaded_weight.clone()
        output_dim = getattr(param, "output_dim", None)
        # Special case for AQLM codebooks.
        is_metadata = getattr(param, "is_metadata", False)

        param_shard_splitter = getattr(param, "shard_splitter", None)

        if output_dim is not None and param_shard_splitter is not None:
            raise NotImplementedError(
                "We do not currently support output_dim != None and "
                "shard_splitter != None for a parameter. Please open an issue."
            )
        # If a parameter has defined a shard_splitter to be used for
        # the weight, it should be applied before the weight is
        # loaded/copied to the parameter. The shard_splitter applies
        # logic by using the loaded_shard_id to ensure that the loaded
        # param is loaded to the correct location
        # within the parameter defined by the linear method.
        if loaded_shard_id is None and param_shard_splitter is not None:
            raise NotImplementedError(
                "We do not currently support loaded_shard_id == None and "
                "shard_splitter != None for a parameter. Please open an issue."
            )

        # Special case for Fp8 scales.
        fp8_scales_shard_indexer = getattr(param, "fp8_scales_shard_indexer",
                                           None)

        if loaded_shard_id is None:
            # Loaded weight is already packed.
            if output_dim is None:
                assert param_data.shape == loaded_weight.shape
                param_data.copy_(loaded_weight)
                return
            """
            shard_offsets = []
            # Q Matrices
            shard_offsets.append(("q", 0, self.total_num_heads * self.head_size))
            current_shard_offset = self.total_num_heads * self.head_size
            # K matrices
            for i in range(self.total_num_kv_heads):
                shard_offsets.append((i, current_shard_offset, self.head_size))
                current_shard_offset += self.head_size
            # V matrices
            shard_offsets.append(("v", current_shard_offset, self.total_num_kv_heads * self.head_size))
"""
            shard_offsets = [
                # (shard_id, shard_offset, shard_size)
                ("q", 0, self.total_num_heads * self.head_size),
                ("k", self.total_num_heads * self.head_size,
                 self.total_num_kv_heads * self.head_size),
                ("v", (self.total_num_heads + self.total_num_kv_heads) *
                 self.head_size, self.total_num_kv_heads * self.head_size),
            ]
            packed_dim = getattr(param, "packed_dim", None)
            for shard_id, shard_offset, shard_size in shard_offsets:
                # Special case for Quantized Weights.
                # If quantized, we need to adjust the offset and size to account
                # for the packing.
                if packed_dim == output_dim:
                    shard_size = shard_size // param.pack_factor
                    shard_offset = shard_offset // param.pack_factor

                    # Special case for Marlin.
                    shard_size, shard_offset = adjust_marlin_shard(
                        param, shard_size, shard_offset)

                loaded_weight_shard = loaded_weight.narrow(
                    output_dim, shard_offset, shard_size)
                self.weight_loader(param, loaded_weight_shard, shard_id)
            return

        #tp_rank = get_tensor_model_parallel_rank()
        assert loaded_shard_id in ["q", "k", "v"]
        #assert loaded_shard_id in ["q", "k", "v"] or (isinstance(loaded_shard_id, int) and loaded_shard_id < self.total_num_kv_heads)
        tp_rank = get_tensor_model_parallel_rank()
        tp_size = get_tensor_model_parallel_world_size()

        num_kv_iterations = 1

        if (loaded_shard_id == "k"):
            num_kv_iterations = self.total_num_kv_heads
        elif (loaded_shard_id == "v"):
            num_kv_iterations = self.total_num_kv_heads
        else:
            num_kv_iterations = 1

        for kv_head in range(num_kv_iterations):
        # If output dim is defined, use the default loading process.
            if output_dim is not None:
                if loaded_shard_id == "q":
                    shard_offset = 0
                    shard_size = self.num_heads * self.head_size
                #elif (isinstance(loaded_shard_id, int) and loaded_shard_id < self.total_num_kv_heads):
                elif loaded_shard_id == "k":
                    shard_offset = (self.num_heads * self.head_size) + ((kv_head * self.head_size) // tp_size)
                    shard_size = (self.head_size) // tp_size
                elif loaded_shard_id == "v":
                    shard_offset = (self.num_heads * self.head_size) + ((self.total_num_kv_heads * self.head_size) // tp_size) + ((kv_head * self.head_size) // tp_size)
                    shard_size = (self.head_size) // tp_size
                    #shard_size = self.num_kv_heads * self.head_size
                # Special case for Quantized Weights.
                # If quantized, we need to adjust the offset and size to account
                # for the packing.
                packed_dim = getattr(param, "packed_dim", None)
                if packed_dim == output_dim:
                    shard_size = shard_size // param.pack_factor
                    shard_offset = shard_offset // param.pack_factor

                    # Special case for Marlin.
                    shard_size, shard_offset = adjust_marlin_shard(
                        param, shard_size, shard_offset)

                param_data = param_unmodified_data.narrow(output_dim, shard_offset,
                                               shard_size)
               # if loaded_shard_id == "v":
               #     shard_id = tp_rank // self.num_kv_head_replicas
               # else:
               #     shard_id = tp_rank
                shard_id = tp_rank
                start_idx = (shard_id * shard_size) + (kv_head * shard_size * tp_size)
                loaded_weight = loaded_unmodified_weight.narrow(output_dim, start_idx,
                                                     shard_size)
            # Special case for for AQLM codebooks.
            # TODO: Not fully sure of about the shard_index calculations
            elif is_metadata:
                # metadata indicates fixed size concatenated along dim 0
                shard_size = loaded_weight.shape[0]
                shard_index = (["q"] + list(range(0, self.total_num_kv_heads)) + ["v"]).index(loaded_shard_id)
                param_data = param_data.narrow(0, shard_index * shard_size,
                                               shard_size)
            # If a param_shard_splitter is defined by the LinearMethod, use it.
            elif param_shard_splitter is not None:
                logical_widths = getattr(param, "logical_widths", None)
                param_data, loaded_weight = param_shard_splitter(
                    param_data, loaded_weight, loaded_shard_id, logical_widths)

            # Special case for Fp8 scales.
            elif fp8_scales_shard_indexer is not None:
                param_data, loaded_weight = fp8_scales_shard_indexer(
                    param_data, loaded_weight, loaded_shard_id)
            else:
                ignore_warning = getattr(param, "ignore_warning", False)
                if not ignore_warning:
                    logger.warning(
                        "Loading a weight without `output_dim` attribute in "
                        "QKVParallelLinear, assume the weight is the same "
                        "for all partitions.")

            if len(param_data.shape) == 0:
                param_data = param_data.reshape(1)

            if len(loaded_weight.shape) == 0:
                loaded_weight = loaded_weight.reshape(1)

            assert param_data.shape == loaded_weight.shape
            param_data.copy_(loaded_weight)

    def forward(self, input_, positions):
        bias = self.bias if not self.skip_bias_add else None
        tp_rank = get_tensor_model_parallel_rank()
        tp_size = get_tensor_model_parallel_world_size()

        positions_as_list = positions.tolist()

#        for each_pos in positions_as_list:
#            print("GPU location: ", each_pos, each_pos%tp_size)

        # Matrix multiply.
        assert self.quant_method is not None
        output_parallel = self.quant_method.apply(self, input_, bias)

        q_sub, k_sub, v_sub = output_parallel.split([self.num_heads * self.head_size, self.num_kv_heads * self.head_size, self.num_kv_heads * self.head_size], dim=-1)

        min_num_tokens_per_GPU = q_sub.size(0) // tp_size
        num_GPUs_with_extra_token = q_sub.size(0) % tp_size

        partition_sizes = []
        for each_slice in range(tp_size):
            append_value = 0
            if (num_GPUs_with_extra_token > 0):
                append_value = 1
                num_GPUs_with_extra_token = num_GPUs_with_extra_token - 1
            append_value = append_value + min_num_tokens_per_GPU
            partition_sizes.append(append_value)


        if (tp_rank == 0):
            print(partition_sizes)

        splitted_k = split_tensor_along_last_dim(
            k_sub, num_partitions=self.total_num_kv_heads)
        #k_slice = splitted_k[tp_rank].contiguous()
        splitted_v = split_tensor_along_last_dim(
            v_sub, num_partitions=self.total_num_kv_heads)

        #k_gathered = tensor_model_parallel_all_gather(k_slice)

        output_par = torch.cat([q_sub], dim=-1)

        for k_h in range(self.total_num_kv_heads):
            k_gathered = tensor_model_parallel_all_gather(splitted_k[k_h].contiguous())
            if (tp_rank == (k_h//self.num_kv_heads)):
                output_par = torch.cat([output_par, k_gathered], dim=-1)
                #print(k_gathered.shape, output_par.shape)
#            if (tp_rank == 0):
#                print(k_gathered)
        for v_h in range(self.total_num_kv_heads):
            v_gathered = tensor_model_parallel_all_gather(splitted_v[v_h].contiguous())
            if (tp_rank == (v_h//self.num_kv_heads)):
                output_par = torch.cat([output_par, v_gathered], dim=-1)
        #output_par = torch.cat([output_par, v_sub], dim=-1)

        if self.gather_output:
            # All-gather across the partitions.
            output = tensor_model_parallel_all_gather(output_par)
        else:
            output = output_par
        output_bias = self.bias if self.skip_bias_add else None
        return output, output_bias


class RowParallelLinear(LinearBase):
    """Linear layer with row parallelism.

    The linear layer is defined as Y = XA + b. A is parallelized along
    its first dimension and X along its second dimension as:
               -   -
              | A_1 |
              | .   |
          A = | .   |        X = [X_1, ..., X_p]
              | .   |
              | A_p |
               -   -
    Arguments:
        input_size: first dimension of matrix A.
        output_size: second dimension of matrix A.
        bias: If true, add bias. Note that bias is not parallelized.
        input_is_parallel: If true, we assume that the input is already
                           split across the GPUs and we do not split
                           again.
        skip_bias_add: This was added to enable performance optimization where
                       bias can be fused with other element-wise operations.
                       We skip adding bias but instead return it.
        params_dtype: Data type for the parameters.
        quant_config: Quantization configure.
    """

    def __init__(self,
                 input_size: int,
                 output_size: int,
                 bias: bool = True,
                 input_is_parallel: bool = True,
                 skip_bias_add: bool = False,
                 params_dtype: Optional[torch.dtype] = None,
                 reduce_results: bool = True,
                 quant_config: Optional[QuantizationConfig] = None):
        super().__init__(input_size, output_size, skip_bias_add, params_dtype,
                         quant_config)

        self.input_is_parallel = input_is_parallel
        self.reduce_results = reduce_results

        # Divide the weight matrix along the last dimension.
        self.tp_size = get_tensor_model_parallel_world_size()
        self.input_size_per_partition = divide(input_size, self.tp_size)
        assert self.quant_method is not None
        self.quant_method.create_weights(
            layer=self,
            input_size_per_partition=self.input_size_per_partition,
            output_partition_sizes=[self.output_size],
            input_size=self.input_size,
            output_size=self.output_size,
            params_dtype=self.params_dtype,
            weight_loader=self.weight_loader)
        if not reduce_results and (bias and not skip_bias_add):
            raise ValueError("When not reduce the results, adding bias to the "
                             "results can lead to incorrect results")

        if bias:
            self.bias = Parameter(
                torch.empty(self.output_size, dtype=params_dtype))
            set_weight_attrs(self.bias, {
                "output_dim": 0,
                "weight_loader": self.weight_loader,
            })
        else:
            self.register_parameter("bias", None)

    def weight_loader(self, param: Parameter, loaded_weight: torch.Tensor):
        # Special case for Fp8 scales.
        fp8_scales_shard_indexer = getattr(param, "fp8_scales_shard_indexer",
                                           None)

        tp_rank = get_tensor_model_parallel_rank()
        input_dim = getattr(param, "input_dim", None)
        param_data = param.data
        if input_dim is not None:
            shard_size = param_data.shape[input_dim]
        #    print("*******")
        #    print(shard_size, param_data.shape, loaded_weight.shape)
        #    print("*******")
            start_idx = tp_rank * shard_size
            loaded_weight = loaded_weight.narrow(input_dim, start_idx,
                                                 shard_size)

        # Special case for Fp8 scales.
        elif fp8_scales_shard_indexer is not None:
            param_data, loaded_weight = fp8_scales_shard_indexer(param_data,
                                                                 loaded_weight,
                                                                 shard_id=0)

        if fp8_scales_shard_indexer is None and len(loaded_weight.shape) == 0:
            loaded_weight = loaded_weight.reshape(1)

        assert param_data.shape == loaded_weight.shape
        param_data.copy_(loaded_weight)

    def forward(self, input_):
        # Set up backprop all-reduce.
        if self.input_is_parallel:
            input_parallel = input_
        else:
            tp_rank = get_tensor_model_parallel_rank()
            splitted_input = split_tensor_along_last_dim(
                input_, num_partitions=self.tp_size)
            input_parallel = splitted_input[tp_rank].contiguous()

        # Matrix multiply.
        assert self.quant_method is not None
        output_parallel = self.quant_method.apply(self, input_parallel)
        if self.reduce_results and self.tp_size > 1:
            output_ = tensor_model_parallel_all_reduce(output_parallel)
        else:
            output_ = output_parallel

        if not self.skip_bias_add:
            output = output_ + self.bias if self.bias is not None else output_
            output_bias = None
        else:
            output = output_
            output_bias = self.bias
        return output, output_bias

    def extra_repr(self) -> str:
        s = f"input_features={self.input_size_per_partition}"
        s += f", output_features={self.output_size}"
        s += f", bias={self.bias is not None}"
        s += f", tp_size={self.tp_size}"
        s += f", reduce_results={self.reduce_results}"
        return s
