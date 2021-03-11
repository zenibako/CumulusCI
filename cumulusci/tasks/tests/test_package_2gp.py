from unittest import mock
import io
import json
import os
import pathlib
import shutil
import zipfile

from pydantic import ValidationError
import pytest
import responses
import yaml

from cumulusci.core.config import UniversalConfig
from cumulusci.core.config import BaseProjectConfig
from cumulusci.core.config import OrgConfig
from cumulusci.core.config import TaskConfig
from cumulusci.core.keychain import BaseProjectKeychain
from cumulusci.core.exceptions import DependencyLookupError, GithubException
from cumulusci.core.exceptions import PackageUploadFailure
from cumulusci.core.exceptions import TaskOptionsError
from cumulusci.salesforce_api.package_zip import BasePackageZipBuilder
from cumulusci.tasks.package_2gp import (
    CreatePackageVersion,
    PackageConfig,
    PackageTypeEnum,
    PackageVersionNumber,
    VersionTypeEnum,
)
from cumulusci.utils import temporary_dir
from cumulusci.utils import touch


@pytest.fixture
def repo_root():
    with temporary_dir() as path:
        os.mkdir(".git")
        os.mkdir("src")
        pathlib.Path(path, "src", "package.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<Package xmlns="http://soap.sforce.com/2006/04/metadata"></Package>'
        )
        with open("cumulusci.yml", "w") as f:
            yaml.dump(
                {
                    "project": {
                        "dependencies": [
                            {
                                "name": "EDA unpackaged/pre/first",
                                "repo_owner": "SalesforceFoundation",
                                "repo_name": "EDA",
                                "subfolder": "unpackaged/pre/first",
                            },
                            {
                                "namespace": "hed",
                                "version": "1.99",
                                "dependencies": [
                                    {"namespace": "pub", "version": "1.5"}
                                ],
                            },
                        ]
                    }
                },
                f,
            )
        pathlib.Path(path, "unpackaged", "pre", "first").mkdir(parents=True)
        touch(os.path.join("unpackaged", "pre", "first", "package.xml"))
        yield path


@pytest.fixture
def project_config(repo_root):
    project_config = BaseProjectConfig(
        UniversalConfig(),
        repo_info={"root": repo_root, "branch": "main"},
    )
    project_config.config["project"]["package"]["install_class"] = "Install"
    project_config.config["project"]["package"]["uninstall_class"] = "Uninstall"
    project_config.keychain = BaseProjectKeychain(project_config, key=None)
    pathlib.Path(repo_root, "orgs").mkdir()
    pathlib.Path(repo_root, "orgs", "scratch_def.json").write_text(
        json.dumps(
            {
                "edition": "Developer",
                "settings": {},
            }
        )
    )

    project_config.get_github_api = mock.Mock()

    return project_config


@pytest.fixture
def devhub_config():
    org_config = OrgConfig(
        {"instance_url": "https://devhub.my.salesforce.com", "access_token": "token"},
        "devhub",
    )
    org_config.refresh_oauth_token = mock.Mock()
    return org_config


@pytest.fixture
def org_config():
    org_config = OrgConfig(
        {
            "instance_url": "https://scratch.my.salesforce.com",
            "access_token": "token",
            "config_file": "orgs/scratch_def.json",
        },
        "dev",
    )
    org_config.refresh_oauth_token = mock.Mock()
    return org_config


@pytest.fixture
def task(project_config, devhub_config, org_config):
    task = CreatePackageVersion(
        project_config,
        TaskConfig(
            {
                "options": {
                    "package_type": "Managed",
                    "org_dependent": False,
                    "package_name": "Test Package",
                    "static_resource_path": "static-resources",
                }
            }
        ),
        org_config,
    )
    with mock.patch(
        "cumulusci.tasks.package_2gp.get_devhub_config", return_value=devhub_config
    ):
        task._init_task()
    return task


@pytest.fixture
def mock_download_extract_github():
    with mock.patch(
        "cumulusci.tasks.package_2gp.download_extract_github"
    ) as download_extract_github:
        yield download_extract_github


class TestPackageVersionNumber:
    def test_parse_format(self):
        assert PackageVersionNumber.parse("1.2.3.4").format() == "1.2.3.4"

    def test_parse__invalid(self):
        with pytest.raises(ValueError):
            PackageVersionNumber.parse("asdf")

    def test_increment(self):
        assert (
            PackageVersionNumber.parse("1.0").increment(VersionTypeEnum.major).format()
            == "2.0.0.NEXT"
        )
        assert (
            PackageVersionNumber.parse("1.0").increment(VersionTypeEnum.minor).format()
            == "1.1.0.NEXT"
        )
        assert (
            PackageVersionNumber.parse("1.0").increment(VersionTypeEnum.patch).format()
            == "1.0.1.NEXT"
        )


class TestPackageConfig:
    def test_validate_org_dependent(self):
        with pytest.raises(ValidationError, match="Only unlocked packages"):
            PackageConfig(package_type=PackageTypeEnum.managed, org_dependent=True)

    def test_validate_post_install_script(self):
        with pytest.raises(ValidationError, match="Only managed packages"):
            PackageConfig(
                package_type=PackageTypeEnum.unlocked, post_install_script="Install"
            )

    def test_validate_uninstall_script(self):
        with pytest.raises(ValidationError, match="Only managed packages"):
            PackageConfig(
                package_type=PackageTypeEnum.unlocked, uninstall_script="Uninstall"
            )


class TestCreatePackageVersion:
    devhub_base_url = "https://devhub.my.salesforce.com/services/data/v50.0"
    scratch_base_url = "https://scratch.my.salesforce.com/services/data/v50.0"

    @responses.activate
    def test_run_task(self, task, mock_download_extract_github, devhub_config):
        mock_download_extract_github.return_value = zipfile.ZipFile(io.BytesIO(), "w")

        responses.add(  # query to find existing package
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={"size": 0, "records": []},
        )
        responses.add(  # create Package2
            "POST",
            f"{self.devhub_base_url}/tooling/sobjects/Package2/",
            json={"id": "0Ho6g000000fy4ZCAQ"},
        )
        responses.add(  # query to find existing Package2VersionCreateRequest
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={"size": 0, "records": []},
        )
        responses.add(  # query to find base version
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={
                "size": 1,
                "records": [
                    {
                        "Id": "04t000000000002AAA",
                        "MajorVersion": 1,
                        "MinorVersion": 0,
                        "PatchVersion": 0,
                        "BuildNumber": 1,
                        "IsReleased": False,
                    }
                ],
            },
        )
        responses.add(  # get dependency org API version
            "GET",
            "https://scratch.my.salesforce.com/services/data",
            json=[{"version": "50.0"}],
        )
        responses.add(  # query for dependency org installed packages
            "GET",
            f"{self.scratch_base_url}/tooling/query/",
            json={
                "size": 1,
                "records": [
                    {
                        "SubscriberPackage": {
                            "Id": "033000000000002AAA",
                            "NamespacePrefix": "pub",
                        },
                        "SubscriberPackageVersionId": "04t000000000002AAA",
                    },
                    {
                        "SubscriberPackage": {
                            "Id": "033000000000003AAA",
                            "NamespacePrefix": "hed",
                        },
                        "SubscriberPackageVersionId": "04t000000000003AAA",
                    },
                ],
            },
        )
        responses.add(  # query dependency org for installed package 1)
            "GET",
            f"{self.scratch_base_url}/tooling/query/",
            json={
                "size": 1,
                "records": [
                    {
                        "Id": "04t000000000002AAA",
                        "MajorVersion": 1,
                        "MinorVersion": 5,
                        "PatchVersion": 0,
                        "BuildNumber": 1,
                        "IsBeta": False,
                    }
                ],
            },
        ),
        responses.add(  # query dependency org for installed package 2)
            "GET",
            f"{self.scratch_base_url}/tooling/query/",
            json={
                "size": 1,
                "records": [
                    {
                        "Id": "04t000000000003AAA",
                        "MajorVersion": 1,
                        "MinorVersion": 99,
                        "PatchVersion": 0,
                        "BuildNumber": 1,
                        "IsBeta": False,
                    }
                ],
            },
        )
        responses.add(  # query for existing package (dependency from github)
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={
                "size": 1,
                "records": [
                    {"Id": "0Ho000000000001AAA", "ContainerOptions": "Unlocked"}
                ],
            },
        )
        responses.add(  # query for existing package version (dependency from github)
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={"size": 1, "records": [{"Id": "08c000000000001AAA"}]},
        )
        responses.add(  # check status of Package2VersionCreateRequest (dependency from github)
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={
                "size": 1,
                "records": [
                    {
                        "Id": "08c000000000001AAA",
                        "Status": "Success",
                        "Package2VersionId": "051000000000001AAA",
                    }
                ],
            },
        )
        responses.add(  # get info from Package2Version (dependency from github)
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={
                "size": 1,
                "records": [
                    {
                        "SubscriberPackageVersionId": "04t000000000001AAA",
                        "MajorVersion": 0,
                        "MinorVersion": 1,
                        "PatchVersion": 0,
                        "BuildNumber": 1,
                    }
                ],
            },
        )
        responses.add(  # query for existing package (unpackaged/pre)
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={
                "size": 1,
                "records": [
                    {"Id": "0Ho000000000004AAA", "ContainerOptions": "Unlocked"}
                ],
            },
        )
        responses.add(  # query for existing package version (unpackaged/pre)
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={"size": 1, "records": [{"Id": "08c000000000004AAA"}]},
        )
        responses.add(  # check status of Package2VersionCreateRequest (unpackaged/pre)
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={
                "size": 1,
                "records": [
                    {
                        "Id": "08c000000000004AAA",
                        "Status": "Success",
                        "Package2VersionId": "051000000000004AAA",
                    }
                ],
            },
        )
        responses.add(  # get info from Package2Version (unpackaged/pre)
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={
                "size": 1,
                "records": [
                    {
                        "SubscriberPackageVersionId": "04t000000000004AAA",
                        "MajorVersion": 0,
                        "MinorVersion": 1,
                        "PatchVersion": 0,
                        "BuildNumber": 1,
                    }
                ],
            },
        )
        responses.add(  # create Package2VersionCreateRequest (main package)
            "POST",
            f"{self.devhub_base_url}/tooling/sobjects/Package2VersionCreateRequest/",
            json={"id": "08c000000000002AAA"},
        )
        responses.add(  # check status of Package2VersionCreateRequest (main package)
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={
                "size": 1,
                "records": [
                    {
                        "Id": "08c000000000002AAA",
                        "Status": "Success",
                        "Package2VersionId": "051000000000002AAA",
                    }
                ],
            },
        )
        responses.add(  # get info from Package2Version (main package)
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={
                "size": 1,
                "records": [
                    {
                        "SubscriberPackageVersionId": "04t000000000002AAA",
                        "MajorVersion": 1,
                        "MinorVersion": 0,
                        "PatchVersion": 0,
                        "BuildNumber": 1,
                    }
                ],
            },
        )
        responses.add(  # get dependencies from SubscriberPackageVersion (main package)
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={"size": 1, "records": [{"Dependencies": ""}]},
        )

        with mock.patch(
            "cumulusci.tasks.package_2gp.get_devhub_config", return_value=devhub_config
        ):
            task()

    @responses.activate
    def test_get_or_create_package__namespaced_existing(
        self, project_config, devhub_config, org_config
    ):
        responses.add(  # query to find existing package
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={
                "size": 1,
                "records": [
                    {"Id": "0Ho6g000000fy4ZCAQ", "ContainerOptions": "Managed"}
                ],
            },
        )

        task = CreatePackageVersion(
            project_config,
            TaskConfig(
                {
                    "options": {
                        "package_type": "Managed",
                        "package_name": "Test Package",
                        "namespace": "ns",
                    }
                }
            ),
            org_config,
        )

        with mock.patch(
            "cumulusci.tasks.package_2gp.get_devhub_config", return_value=devhub_config
        ):
            task._init_task()

        result = task._get_or_create_package(task.package_config)
        assert result == "0Ho6g000000fy4ZCAQ"

    @responses.activate
    def test_get_or_create_package__exists_but_wrong_type(
        self, project_config, devhub_config, org_config
    ):
        responses.add(  # query to find existing package
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={
                "size": 1,
                "records": [
                    {"Id": "0Ho6g000000fy4ZCAQ", "ContainerOptions": "Unlocked"}
                ],
            },
        )

        task = CreatePackageVersion(
            project_config,
            TaskConfig(
                {
                    "options": {
                        "package_type": "Managed",
                        "package_name": "Test Package",
                        "namespace": "ns",
                    }
                }
            ),
            org_config,
        )
        with mock.patch(
            "cumulusci.tasks.package_2gp.get_devhub_config", return_value=devhub_config
        ):
            task._init_task()
        with pytest.raises(PackageUploadFailure):
            task._get_or_create_package(task.package_config)

    @responses.activate
    def test_get_or_create_package__devhub_disabled(self, task):
        responses.add(
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json=[{"message": "Object type 'Package2' is not supported"}],
            status=400,
        )

        with pytest.raises(TaskOptionsError):
            task._get_or_create_package(task.package_config)

    @responses.activate
    def test_get_or_create_package__multiple_existing(self, task):
        responses.add(
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={"size": 2, "records": []},
        )

        with pytest.raises(TaskOptionsError):
            task._get_or_create_package(task.package_config)

    @responses.activate
    def test_create_version_request__existing_package_version(self, task):
        responses.add(
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={"size": 1, "records": [{"Id": "08c000000000001AAA"}]},
        )

        builder = BasePackageZipBuilder()
        result = task._create_version_request(
            "0Ho6g000000fy4ZCAQ", task.package_config, builder
        )
        assert result == "08c000000000001AAA"

    def test_has_1gp_namespace_dependencies__no(self, task):
        assert not task._has_1gp_namespace_dependency([])

    def test_has_1gp_namespace_dependencies__transitive(self, task):
        assert task._has_1gp_namespace_dependency(
            [{"dependencies": [{"namespace": "foo", "version": "1.0"}]}]
        )

    def test_convert_project_dependencies__unrecognized_format(self, task):
        with pytest.raises(DependencyLookupError):
            task._convert_project_dependencies([{"foo": "bar"}])

    def test_unpackaged_pre_dependencies__none(self, task):
        shutil.rmtree(str(pathlib.Path(task.project_config.repo_root, "unpackaged")))

        assert task._get_unpackaged_pre_dependencies([]) == []

    @responses.activate
    def test_poll_action__error(self, task):
        responses.add(
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={
                "size": 1,
                "records": [{"Id": "08c000000000002AAA", "Status": "Error"}],
            },
        )
        responses.add(
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={"size": 1, "records": [{"Message": "message"}]},
        )

        task.request_id = "08c000000000002AAA"
        with pytest.raises(PackageUploadFailure) as err:
            task._poll_action()
        assert "message" in str(err)

    @responses.activate
    def test_poll_action__other(self, task):
        responses.add(
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={
                "size": 1,
                "records": [{"Id": "08c000000000002AAA", "Status": "InProgress"}],
            },
        )

        task.request_id = "08c000000000002AAA"
        task._poll_action()

    @responses.activate
    def test_get_base_version_number__fallback(self, task):
        responses.add(
            "GET",
            f"{self.devhub_base_url}/tooling/query/",
            json={"size": 0, "records": []},
        )

        version = task._get_base_version_number(None, "0Ho6g000000fy4ZCAQ")
        assert version.format() == "0.0.0.0"

    @responses.activate
    def test_get_base_version_number__from_github(self, task):
        task.project_config.get_latest_version = mock.Mock(return_value="1.0")

        version = task._get_base_version_number(
            "latest_github_release", "0Ho6g000000fy4ZCAQ"
        )
        assert version.format() == "1.0.0.0"

    @responses.activate
    def test_get_base_version_number__from_github__no_release(self, task):
        task.project_config.get_latest_version = mock.Mock(side_effect=GithubException)

        version = task._get_base_version_number(
            "latest_github_release", "0Ho6g000000fy4ZCAQ"
        )
        assert version.format() == "0.0.0.0"

    @responses.activate
    def test_get_base_version_number__explicit(self, task):
        version = task._get_base_version_number("1.0", "0Ho6g000000fy4ZCAQ")
        assert version.format() == "1.0.0.0"
