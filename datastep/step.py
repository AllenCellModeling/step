#!/usr/bin/env python
# -*- coding: utf-8 -*-

import getpass
import inspect
import json
import logging
import os
import warnings
from functools import wraps
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional, Union

import git
import pandas as pd
import prefect
import quilt3
from prefect import Flow, Task

from . import constants, exceptions, file_utils, get_module_version, quilt_utils

###############################################################################

log = logging.getLogger(__name__)

###############################################################################


# decorator for run that logs non default args and kwargs to file
def log_run_params(func):
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        # Get the params for the function, not the wrapper
        params = inspect.signature(func).bind(self, *args, **kwargs).arguments
        params.pop("self")

        # In the case the operation is happening in a distributed fashion
        # Always make the local staging dir prior to run
        self.step_local_staging_dir.mkdir(parents=True, exist_ok=True)
        parameter_store = self.step_local_staging_dir / "run_parameters.json"

        # Dump run params
        with open(parameter_store, "w") as write_out:
            json.dump(params, write_out, default=str)
            log.debug(f"Stored params for run at: {parameter_store}")

        # Check if we want to clean the step local staging prior to run
        # If the user has defined clean in their run function it will be in
        # top level params, if they haven't it will be in the kwargs
        if "clean" in params and params["clean"]:
            file_utils._clean(self.step_local_staging_dir)
            log.info(f"Cleaned directory: {self.step_local_staging_dir}")
        elif "kwargs" in params:
            if "clean" in params["kwargs"] and params["kwargs"]["clean"]:
                file_utils._clean(self.step_local_staging_dir)
                log.info(f"Cleaned directory: {self.step_local_staging_dir}")

        return func(self, *args, **kwargs)

    return wrapper


class Step(Task):
    """
    A class for creating "pure function" steps in a DAG.

    This object's sole purpose is to handle and enforce data logging tied to code using
    Quilt.

    It manages to do this data logging through heavy utilization of a local staging
    directory and supporting files such as initialization parameters, a manifest CSV
    that you can use to store the files you will want to send to Quilt.

    However, as a part of the problem with stepwise workflows is their dependents on
    upstream data is hard to manage, the more you rely on this object the easier those
    upstream dependecies become. As if your upstream data dependecies are generated by
    other Step modules, then you can place them in the downstream Step as
    "direct_upstream_tasks" and use the `Step.pull` function to retrieve their data.

    Parameters
    ----------
    step_name: Optional[str]
        A name for this step.
        Default: the lowercased version of the inheriting object name
    filepath_columns: List[str]
        In the final manifest CSV you generate, which columns store filepaths.
        Default: ["filepath"]
    metadata_columns: List[str]
        In the final manifest CSV you generate, which columns store metadata.
        Default: []
    direct_upstream_tasks: List[Step]
        If you need data for this task to run, and that data was generated by another
        Step object you can place references to those objects here and during the
        pull method this Step will retrieve the required data.
    config: Optional[Union[str, Path, Dict[str, str]]]
        A path or dictionary detailing the entire workflow config.
        Refer to `datastep.constants` for details on workflow config defaults.
    """

    def _unpack_config(self, config: Optional[Union[str, Path, Dict[str, str]]] = None):
        # If not provided, check for other places the config could live
        if config is None:
            # Check environment
            if constants.CONFIG_ENV_VAR_NAME in os.environ:
                config = os.environ[constants.CONFIG_ENV_VAR_NAME]

            # Check current working directory
            else:
                cwd = Path().resolve()
                cwd_files = [str(f.name) for f in cwd.iterdir()]

                # Attach config file name to cwd path
                if constants.CWD_CONFIG_FILE_NAME in cwd_files:
                    config = cwd / constants.CWD_CONFIG_FILE_NAME

        # Config should now either be path to JSON, Dict, or None
        if isinstance(config, (str, Path)):
            # Resolve path
            config = file_utils.resolve_filepath(config)

            # Read config
            with open(config, "r") as read_in:
                config = json.load(read_in)

        # Config should now either have been provided as a dict, parsed, or None
        if isinstance(config, dict):
            # Get or default storage bucket
            config["quilt_storage_bucket"] = config.get(
                "quilt_storage_bucket", constants.DEFAULT_QUILT_STORAGE
            )

            # Get or default package owner
            config["quilt_package_owner"] = config.get(
                "quilt_package_owner", constants.DEFAULT_QUILT_PACKAGE_OWNER
            )

            # Get or default project local staging
            config["project_local_staging_dir"] = file_utils.resolve_directory(
                config.get(
                    "project_local_staging_dir",
                    constants.DEFAULT_PROJECT_LOCAL_STAGING_DIR.format(cwd="."),
                ),
                make=True,
                strict=False,
            )

            # Get or default step local staging
            if self.step_name in config:
                config[self.step_name][
                    "step_local_staging_dir"
                ] = file_utils.resolve_directory(
                    config[self.step_name].get(
                        "step_local_staging_dir",
                        f"{config['project_local_staging_dir'] / self.step_name}",
                    ),
                    make=True,
                    strict=False,
                )
            else:
                # Step name wasn't in the config, add it as a key to a further dict
                config[self.step_name] = {}
                config[self.step_name][
                    "step_local_staging_dir"
                ] = file_utils.resolve_directory(
                    f"{config['project_local_staging_dir'] / self.step_name}",
                    make=True,
                    strict=False,
                )

            # Get or default quilt package name
            config["quilt_package_name"] = file_utils._sanitize_name(
                config.get("quilt_package_name", self.__module__.split(".")[0])
            )

            log.debug(f"Unpacked config: {config}")

        else:
            # Log debug message indicating using defaults
            log.debug(f"Using default project and step configuration.")

            # Construct config dictionary object
            config = {
                "quilt_storage_bucket": constants.DEFAULT_QUILT_STORAGE,
                "quilt_package_owner": constants.DEFAULT_QUILT_PACKAGE_OWNER,
                "quilt_package_name": self.__module__.split(".")[0],
                "project_local_staging_dir": file_utils.resolve_directory(
                    constants.DEFAULT_PROJECT_LOCAL_STAGING_DIR.format(cwd="."),
                    make=True,
                    strict=False,
                ),
                self.step_name: {
                    "step_local_staging_dir": file_utils.resolve_directory(
                        constants.DEFAULT_STEP_LOCAL_STAGING_DIR.format(
                            cwd=".", module_name=self.step_name
                        ),
                        make=True,
                        strict=False,
                    )
                },
            }

        # Set object properties from config
        self._storage_bucket = config["quilt_storage_bucket"]
        self._quilt_package_owner = config["quilt_package_owner"]
        self._quilt_package_name = config["quilt_package_name"]
        self._project_local_staging_dir = config["project_local_staging_dir"]
        self._step_local_staging_dir = config[self.step_name]["step_local_staging_dir"]

        return config

    def __init__(
        self,
        step_name: Optional[str] = None,
        filepath_columns: List[str] = ["filepath"],
        metadata_columns: List[str] = [],
        direct_upstream_tasks: List["Step"] = [],
        config: Optional[Union[str, Path, Dict[str, str]]] = None,
        **kwargs,
    ):
        # Run super prefect Task init
        super().__init__(**kwargs)

        # Set step name as attributes if not None
        self._step_name = (
            file_utils._sanitize_name(step_name)
            if step_name is not None
            else self.__class__.__name__.lower()
        )

        # Set kwargs as attributes
        self._upstream_tasks = direct_upstream_tasks
        self.filepath_columns = filepath_columns
        self.metadata_columns = metadata_columns

        # Prepare locals to be stored for data logging
        params = locals()
        params["step_name"] = self._step_name
        params.pop("self")
        params.pop("__class__")

        # Unpack config into param log dict
        params["config"] = self._unpack_config(config)

        # Store current version of datastep in initialization parameters
        params["__version__"] = get_module_version()

        # Write out initialization params for data logging
        parameter_store = self.step_local_staging_dir / "init_parameters.json"
        with open(parameter_store, "w") as write_out:
            json.dump(params, write_out, default=str)
            log.debug(f"Stored params for run at: {parameter_store}")

        # Attempt to read a previously written manifest produced by this step
        m_path = Path(self.step_local_staging_dir / "manifest.csv")

        # Check if a prior manifest exists
        if m_path.is_file():
            self.manifest = pd.read_csv(m_path)
            log.debug(f"Read previously produced manifest from file: {m_path}")
        else:
            self.manifest = None
            log.debug(f"No previous manifest found. Checked path: {m_path}")

        # Set name for prefect task retrieval
        self.name = self.step_name

        # Prior to any operation log where we are operating
        log.info(
            f"{self.step_name} will use step local staging directory: "
            f"{self.step_local_staging_dir}"
        )

    @property
    def step_name(self) -> str:
        """
        Return the name of this step as a string.
        """
        return self._step_name

    @property
    def upstream_tasks(self) -> List[str]:
        warnings.warn(
            "To enforce that there is no reliance on object state during run "
            "functions, the upstream_tasks property will be deprecated on the "
            "next datastep release.",
            PendingDeprecationWarning,
        )
        return self._upstream_tasks

    @property
    def storage_bucket(self) -> str:
        warnings.warn(
            "To enforce that there is no reliance on object state during run "
            "functions, the storage_bucket property will be deprecated on the "
            "next datastep release.",
            PendingDeprecationWarning,
        )
        return self._storage_bucket

    @property
    def project_local_staging_dir(self) -> Path:
        warnings.warn(
            "To enforce that there is no reliance on object state during run "
            "functions, the project_local_staging_dir property will be deprecated "
            "on the next datastep release.",
            PendingDeprecationWarning,
        )
        return self._project_local_staging_dir

    @property
    def step_local_staging_dir(self) -> Path:
        """
        A preconfigured directory for you to store output files in.
        Can be specifically set using a workflow_config.json file.
        """
        return self._step_local_staging_dir

    @property
    def quilt_package_name(self) -> str:
        warnings.warn(
            "To enforce that there is no reliance on object state during run "
            "functions, the quilt_package_name property will be deprecated on the "
            "next datastep release.",
            PendingDeprecationWarning,
        )
        return self._package_name

    @property
    def quilt_package_owner(self) -> str:
        warnings.warn(
            "To enforce that there is no reliance on object state during run "
            "functions, the quilt_package_owner property will be deprecated on the "
            "next datastep release.",
            PendingDeprecationWarning,
        )
        return self._quilt_package_owner

    def run(
        self,
        distributed_executor_address: Optional[str] = None,
        clean: bool = False,
        debug: bool = False,
        **kwargs,
    ) -> Any:
        """
        Run a pure function.

        There are a few "protected" parameters that are the following:

        Parameters
        ----------
        distributed_executor_address: Optional[str]
            An optional executor address to pass to some computation engine.
        clean: bool
            Should the local staging directory be cleaned prior to this run.
            Default: False (Do not clean)
        debug: bool
            A debug flag for the developer to use to manipulate how much data runs,
            how it is processed, etc.
            Default: False (Do not debug)

        Returns
        -------
        result: Any
            A pickable object or value that is the result of any processing you do.
        """
        # Your code here
        #
        # The `self.step_local_staging_dir` is exposed to save files in
        #
        # The user should set `self.manifest` to a dataframe of absolute paths that
        # point to the created files and each files metadata
        #
        # By default, `self.filepath_columns` is ["filepath"], but should be edited
        # if there are more than a single column of filepaths
        #
        # By default, `self.metadata_columns` is [], but should be edited to include
        # any columns that should be parsed for metadata and attached to objects
        #
        # The user should not rely on object state to retrieve results from prior steps.
        # I.E. do not call use the attribute self.upstream_tasks to retrieve data.
        # Pass the required path to a directory of files, the path to a prior manifest,
        # or in general, the exact parameters required for this function to run.
        return

    def get_result(self, state: prefect.engine.state.State, flow: Flow) -> Any:
        """
        Get the result of this step.

        Parameters
        ----------
        state: prefect.engine.state.State
            The final state object of a prefect flow produced by running the flow.
        flow: prefect.core.flow.Flow
            The flow that ran this step.

        Returns
        -------
        result: Any
            The resulting object from running this step in a flow.

        Notes
        -----
        This will always return the first item that matches this step.
        What this means for the user is that if this step was used in a mapped task,
        you would only recieve the result of the first iteration of that map.

        Generally though, you shouldn't be using these steps in mapped tasks.
        (It's on our to-do list...)
        """
        return state.result[flow.get_tasks(name=self.step_name)[0]].result

    def pull(self, data_version: Optional[str] = None, bucket: Optional[str] = None):
        """
        Pull all upstream data dependecies using the list of upstream steps.

        Parameters
        ----------
        data_version: Optional[str]
            Request a specific version of the upstream data.
            Default: 'latest' for all upstreams
        bucket: Optional[str]
            Request data from a specific bucket different from the bucket defined
            by your workflow_config.json or the defaulted bucket.
        """
        # Resolve None bucket
        if bucket is None:
            bucket = self._storage_bucket

        # Run checkout for each upstream
        for UpstreamTask in self._upstream_tasks:
            upstream_task = UpstreamTask()
            upstream_task.checkout(data_version=data_version, bucket=bucket)

    @staticmethod
    def _get_current_git_branch() -> str:
        repo = git.Repo(Path(".").expanduser().resolve())
        return repo.active_branch.name

    @staticmethod
    def _check_git_status_is_clean(push_target: str) -> Optional[Exception]:
        # This will throw an error if the current working directory is not a git repo
        repo = git.Repo(Path(".").expanduser().resolve())
        current_branch = repo.active_branch.name

        # Check current git status
        if repo.is_dirty() or len(repo.untracked_files) > 0:
            dirty_files = [f.b_path for f in repo.index.diff(None)]
            all_changed_files = repo.untracked_files + dirty_files
            raise exceptions.InvalidGitStatus(
                f"Push to '{push_target}' was rejected because the current git "
                f"status of this branch ({current_branch}) is not clean. "
                f"Check files: {all_changed_files}."
            )

        # Check that current hash is the same as remote head hash
        # Check that the current branch has even been pushed to origin
        origin_branches = [b.name for b in repo.remotes.origin.refs]
        if f"origin/{current_branch}" not in origin_branches:
            raise exceptions.InvalidGitStatus(
                f"Push to '{push_target}' was rejected because the current git "
                f"branch was not found on the origin."
            )
        # Origin has current branch, check for matching commit hash
        else:
            # Find matching origin branch
            for origin_branch in repo.remotes.origin.refs:
                if origin_branch.name == f"origin/{current_branch}":
                    matching_origin_branch = origin_branch
                break

            # Check git commit hash match
            if matching_origin_branch.commit.hexsha != repo.head.object.hexsha:
                raise exceptions.InvalidGitStatus(
                    f"Push to '{push_target}' was rejected because the current git "
                    f"commit has not been pushed to {matching_origin_branch.name}"
                )

    @staticmethod
    def _create_data_commit_message() -> str:
        # This will throw an error if the current working directory is not a git repo
        repo = git.Repo(Path(".").expanduser().resolve())
        current_branch = repo.active_branch.name

        return (
            f"data created from code repo {repo.remotes.origin.url} on branch "
            f"{current_branch} at commit {repo.head.object.hexsha}"
        )

    @staticmethod
    def _get_git_origin_url() -> str:
        # This will throw an error if the current working directory is not a git repo
        repo = git.Repo(Path(".").expanduser().resolve())

        # Get origin info
        origin = repo.remotes.origin

        # If there is a @ character this was setup with ssh
        if "@" in origin.url:
            url = origin.url.split("@")[1].replace(":", "/").replace(".git", "")
            return f"https://{url}"
        else:
            return origin.url.replace(".git", "")

    @staticmethod
    def _get_current_git_commit_hash() -> str:
        # This will throw an error if the current working directory is not a git repo
        repo = git.Repo(Path(".").expanduser().resolve())
        return repo.head.object.hexsha

    def manifest_filepaths_rel2abs(self):
        """
        Convert manifest filepaths to absolute paths.

        Useful for after you pull data from a remote bucket.
        """
        self.manifest = file_utils.manifest_filepaths_rel2abs(
            self.manifest, self.filepath_columns, self.step_local_staging_dir
        )

    def manifest_filepaths_abs2rel(self):
        """
        Convert manifest filepaths to relative paths.

        Useful for when you are ready to upload to a remote bucket.
        """
        self.manifest = file_utils.manifest_filepaths_abs2rel(
            self.manifest, self.filepath_columns, self.step_local_staging_dir
        )

    def checkout(
        self, data_version: Optional[str] = None, bucket: Optional[str] = None
    ):
        """
        Pull data previously generated by a run of this step.

        Parameters
        ----------
        data_version: Optional[str]
            Request a specific version of the prior generated data.
            Default: 'latest'
        bucket: Optional[str]
            Request data from a specific bucket different from the bucket defined
            by your workflow_config.json or the defaulted bucket.
        """
        # Resolve None bucket
        if bucket is None:
            bucket = self._storage_bucket

        # Get current git branch
        current_branch = self._get_current_git_branch()

        # Normalize branch name
        # This is to stop quilt from making extra directories from names like:
        # feature/some-feature
        current_branch = current_branch.replace("/", ".")

        # Checkout this step's output from quilt
        # Check for files on this branch and default to master

        # Browse top level project package
        quilt_loc = f"{self._quilt_package_owner}/{self._quilt_package_name}"
        p = quilt3.Package.browse(quilt_loc, bucket, top_hash=data_version)

        # Check to see if step data exists on this branch in quilt
        try:
            quilt_branch_step = f"{current_branch}/{self.step_name}"
            p[quilt_branch_step]

        # If not, use the version on master
        except KeyError:
            quilt_branch_step = f"master/{self.step_name}"
            p[quilt_branch_step]

        # Fetch the data and save it to the local staging dir
        p[quilt_branch_step].fetch(self.step_local_staging_dir)

    def push(self, bucket: Optional[str] = None):
        """
        Push the most recently generated data.

        Parameters
        ----------
        bucket: Optional[str]
            Push data to a specific bucket different from the bucket defined
            by your workflow_config.json or the defaulted bucket.

        Notes
        -----
        If your git status isn't clean, or you haven't commited and pushed to
        origin, any attempt to push data will be rejected.
        """
        # Check if manifest is None
        if self.manifest is None:
            raise exceptions.PackagingError(
                "No manifest found to construct package with."
            )

        # Resolve None bucket
        if bucket is None:
            bucket = self._storage_bucket

        # Get current git branch
        current_branch = self._get_current_git_branch()

        # Normalize branch name
        # This is to stop quilt from making extra directories from names like:
        # feature/some-feature
        current_branch = current_branch.replace("/", ".")

        # Resolve push target
        quilt_loc = f"{self._quilt_package_owner}/{self._quilt_package_name}"
        push_target = f"{quilt_loc}/{current_branch}/{self.step_name}"

        # Check git status is clean
        self._check_git_status_is_clean(push_target)

        # Construct the package
        step_pkg, relative_manifest = quilt_utils.create_package(
            manifest=self.manifest,
            step_pkg_root=self.step_local_staging_dir,
            filepath_columns=self.filepath_columns,
            metadata_columns=self.metadata_columns,
        )

        # Add the relative manifest and generated README to the package
        with TemporaryDirectory() as tempdir:
            # Store the relative manifest in a temporary directory
            m_path = Path(tempdir) / "manifest.csv"
            relative_manifest.to_csv(m_path, index=False)
            step_pkg.set("manifest.csv", m_path)

            # Add the params files to the package
            for param_file in ["run_parameters.json", "init_parameters.json"]:
                param_file_path = self.step_local_staging_dir / param_file
                step_pkg.set(param_file, param_file_path)

            # Generate README
            readme_path = Path(tempdir) / "README.md"
            with open(readme_path, "w") as write_readme:
                write_readme.write(
                    constants.README_TEMPLATE.render(
                        quilt_package_name=self._quilt_package_name,
                        source_url=self._get_git_origin_url(),
                        branch_name=self._get_current_git_branch(),
                        commit_hash=self._get_current_git_commit_hash(),
                        creator=getpass.getuser(),
                    )
                )
            step_pkg.set("README.md", readme_path)

            # Browse top level project package and add / overwrite to it in step dir
            project_pkg = quilt3.Package.browse(quilt_loc, self._storage_bucket)
            for (logical_key, pkg_entry) in step_pkg.walk():
                project_pkg.set(
                    f"{current_branch}/{self.step_name}/{logical_key}", pkg_entry
                )

            # Push the data
            project_pkg.push(
                quilt_loc,
                registry=self._storage_bucket,
                message=self._create_data_commit_message(),
            )

    def clean(self) -> str:
        """
        Completely reset this steps local staging directory by removing all previously
        generated files.
        """
        file_utils._clean(self.step_local_staging_dir)

    def __str__(self):
        return (
            f"<{self.step_name} [ "
            f"upstream_tasks: {self._upstream_tasks}, "
            f"storage_bucket: '{self._storage_bucket}', "
            f"project_local_staging_dir: '{self._project_local_staging_dir}', "
            f"step_local_staging_dir: '{self.step_local_staging_dir}' "
            f"]>"
        )

    def __repr__(self):
        return str(self)
