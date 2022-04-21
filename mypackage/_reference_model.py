from typing import List, Optional
from datetime import date
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
from anndata import AnnData
from pyro import clear_param_store
from scvi import REGISTRY_KEYS
from scvi.data import AnnDataManager
from scvi.data.fields import (
    CategoricalObsField,
    LayerField,
    NumericalJointObsField,
    NumericalObsField,
)
from scvi.model.base import BaseModelClass, PyroSampleMixin, PyroSviTrainMixin
from scvi.utils import setup_anndata_dsp

from cell2location.models.base._pyro_base_reference_module import RegressionBaseModule
from cell2location.models.base._pyro_mixin import PltExportMixin, QuantileMixin
from ._reference_module import RegressionBackgroundDetectionTechPyroModel

class RegressionModel(QuantileMixin, PyroSampleMixin, PyroSviTrainMixin, PltExportMixin, BaseModelClass):
    """
    Model which estimates per cluster average mRNA count account for batch effects. User-end model class.

    https://github.com/BayraktarLab/cell2location

    Parameters
    ----------
    adata
        single-cell AnnData object that has been registered via :func:`~scvi.data.setup_anndata`.
    use_gpu
        Use the GPU?
    **model_kwargs
        Keyword args for :class:`~scvi.external.LocationModelLinearDependentWMultiExperimentModel`

    Examples
    --------
    TODO add example
    >>>
    """

    def __init__(
        self,
        adata: AnnData,
        model_class=None,
        use_average_as_initial: bool = True,
        **model_kwargs,
    ):
        # in case any other model was created before that shares the same parameter names.
        clear_param_store()

        super().__init__(adata)

        if model_class is None:
            model_class = RegressionBackgroundDetectionTechPyroModel

        # use per class average as initial value
#             model_kwargs["init_vals"] = {"per_cluster_mu_fg": aver.values.T.astype("float32") + 0.0001}

        self.module = RegressionBaseModule(
            model=model_class,
            n_obs=self.summary_stats["n_cells"],
            n_vars=self.summary_stats["n_vars"],
            n_batch=self.summary_stats["n_batch"],
            **model_kwargs,
        )
        self._model_summary_string = f'RegressionBackgroundDetectionTech model with the following params: \nn_batch: {self.summary_stats["n_batch"]} '
        self.init_params_ = self._get_init_params(locals())

    @classmethod
    @setup_anndata_dsp.dedent
    def setup_anndata(
        cls,
        adata: AnnData,
        layer: Optional[str] = None,
        batch_key: Optional[str] = None,
        labels_key: Optional[str] = None,
        unspliced_label = 'unspliced',
        spliced_label = 'spliced',
        **kwargs,
    ):
        """
        %(summary)s.

        Parameters
        ----------
        %(param_layer)s
        %(param_batch_key)s
        %(param_labels_key)s
        """
        setup_method_args = cls._get_setup_method_args(**locals())
        adata.obs["_indices"] = np.arange(adata.n_obs).astype("int64")
        anndata_fields = [
            LayerField('unspliced', unspliced_label, is_count_data=True),
            LayerField('spliced', spliced_label, is_count_data=True),
            CategoricalObsField(REGISTRY_KEYS.BATCH_KEY, batch_key),
            NumericalObsField(REGISTRY_KEYS.INDICES_KEY, "_indices"),
        ]
        adata_manager = AnnDataManager(fields=anndata_fields, setup_method_args=setup_method_args)
        adata_manager.register_fields(adata, **kwargs)
        cls.register_manager(adata_manager)

    def train(
        self,
        max_epochs: Optional[int] = None,
        batch_size: int = 2500,
        train_size: float = 1,
        lr: float = 0.002,
        **kwargs,
    ):
        """Train the model with useful defaults

        Parameters
        ----------
        max_epochs
            Number of passes through the dataset. If `None`, defaults to
            `np.min([round((20000 / n_cells) * 400), 400])`
        train_size
            Size of training set in the range [0.0, 1.0].
        batch_size
            Minibatch size to use during training. If `None`, no minibatching occurs and all
            data is copied to device (e.g., GPU).
        lr
            Optimiser learning rate (default optimiser is :class:`~pyro.optim.ClippedAdam`).
            Specifying optimiser via plan_kwargs overrides this choice of lr.
        kwargs
            Other arguments to scvi.model.base.PyroSviTrainMixin().train() method
        """

        kwargs["max_epochs"] = max_epochs
        kwargs["batch_size"] = batch_size
        kwargs["train_size"] = train_size
        kwargs["lr"] = lr

        super().train(**kwargs)
        
    def _export2adata(self, samples):
        r"""
        Export key model variables and samples

        Parameters
        ----------
        samples
            dictionary with posterior mean, 5%/95% quantiles, SD, samples, generated by ``.sample_posterior()``

        Returns
        -------
            Updated dictionary with additional details is saved to ``adata.uns['mod']``.
        """
        # add factor filter and samples of all parameters to unstructured data
        results = {
            "model_name": str(self.module.__class__.__name__),
            "date": str(date.today()),
            "var_names": self.adata.var_names.tolist(),
            "obs_names": self.adata.obs_names.tolist(),
            "post_sample_means": samples["post_sample_means"],
            "post_sample_stds": samples["post_sample_stds"],
            "post_sample_q05": samples["post_sample_q05"],
            "post_sample_q95": samples["post_sample_q95"],
        }

        return results

    def plot_QC(
        self,
        summary_name: str = "means",
        use_n_obs: int = 1000,
        scale_average_detection: bool = True,
    ):
        """
        Show quality control plots:
        1. Reconstruction accuracy to assess if there are any issues with model training.
            The plot should be roughly diagonal, strong deviations signal problems that need to be investigated.
            Plotting is slow because expected value of mRNA count needs to be computed from model parameters. Random
            observations are used to speed up computation.

        2. Estimated reference expression signatures (accounting for batch effect)
            compared to average expression in each cluster. We expect the signatures to be different
            from average when batch effects are present, however, when this plot is very different from
            a perfect diagonal, such as very low values on Y-axis, non-zero density everywhere)
            it indicates problems with signature estimation.

        Parameters
        ----------
        summary_name
            posterior distribution summary to use ('means', 'stds', 'q05', 'q95')

        Returns
        -------

        """

        super().plot_QC(summary_name=summary_name, use_n_obs=use_n_obs)
        plt.show()

        inf_aver = self.samples[f"post_sample_{summary_name}"]["per_cluster_mu_fg"].T
        if scale_average_detection and ("detection_y_c" in list(self.samples[f"post_sample_{summary_name}"].keys())):
            inf_aver = inf_aver * self.samples[f"post_sample_{summary_name}"]["detection_y_c"].mean()
        aver = self._compute_cluster_averages(key=REGISTRY_KEYS.LABELS_KEY)
        aver = aver[self.factor_names_]

        plt.hist2d(
            np.log10(aver.values.flatten() + 1),
            np.log10(inf_aver.flatten() + 1),
            bins=50,
            norm=matplotlib.colors.LogNorm(),
        )
        plt.xlabel("Mean expression for every gene in every cluster")
        plt.ylabel("Estimated expression for every gene in every cluster")
        plt.show()
