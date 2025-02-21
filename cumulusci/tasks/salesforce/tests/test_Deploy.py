import base64
import io
import os
import zipfile

import pytest

from cumulusci.core.exceptions import TaskOptionsError
from cumulusci.core.flowrunner import StepSpec
from cumulusci.core.source_transforms.transforms import CleanMetaXMLTransform
from cumulusci.tasks.salesforce import Deploy
from cumulusci.utils import temporary_dir, touch

from .util import create_task


class TestDeploy:
    @pytest.mark.parametrize("rest_deploy", [True, False])
    def test_get_api(self, rest_deploy):
        with temporary_dir() as path:
            touch("package.xml")
            task = create_task(
                Deploy,
                {
                    "path": path,
                    "namespace_tokenize": "ns",
                    "namespace_inject": "ns",
                    "namespace_strip": "ns",
                    "unmanaged": True,
                    "rest_deply": rest_deploy,
                },
            )

            api = task._get_api()
            zf = zipfile.ZipFile(io.BytesIO(base64.b64decode(api.package_zip)), "r")
            assert "package.xml" in zf.namelist()
            zf.close()

    @pytest.mark.parametrize("rest_deploy", [True, False])
    def test_get_api__managed(self, rest_deploy):
        with temporary_dir() as path:
            touch("package.xml")
            task = create_task(
                Deploy,
                {
                    "path": path,
                    "namespace_inject": "ns",
                    "unmanaged": False,
                    "rest_deploy": rest_deploy,
                },
            )

            api = task._get_api()
            zf = zipfile.ZipFile(io.BytesIO(base64.b64decode(api.package_zip)), "r")
            assert "package.xml" in zf.namelist()
            zf.close()

    @pytest.mark.parametrize("rest_deploy", [True, False])
    def test_get_api__additional_options(self, rest_deploy):
        with temporary_dir() as path:
            touch("package.xml")
            task = create_task(
                Deploy,
                {
                    "path": path,
                    "test_level": "RunSpecifiedTests",
                    "specified_tests": "TestA,TestB",
                    "unmanaged": False,
                    "rest_deploy": rest_deploy,
                },
            )

            api = task._get_api()
            assert api.run_tests == ["TestA", "TestB"]
            assert api.test_level == "RunSpecifiedTests"

    @pytest.mark.parametrize("rest_deploy", [True, False])
    def test_get_api__skip_clean_meta_xml(self, rest_deploy):
        with temporary_dir() as path:
            touch("package.xml")
            task = create_task(
                Deploy,
                {
                    "path": path,
                    "clean_meta_xml": False,
                    "unmanaged": True,
                    "rest_deploy": rest_deploy,
                },
            )

            api = task._get_api()
            zf = zipfile.ZipFile(io.BytesIO(base64.b64decode(api.package_zip)), "r")
            assert "package.xml" in zf.namelist()
            zf.close()

    @pytest.mark.parametrize("rest_deploy", [True, False])
    def test_get_api__static_resources(self, rest_deploy):
        with temporary_dir() as path:
            with open("package.xml", "w") as f:
                f.write(
                    """<?xml version="1.0" encoding="UTF-8"?>
<Package xmlns="http://soap.sforce.com/2006/04/metadata">
    <types>
        <name>OtherType</name>
    </types>
</Package>"""
                )
                touch("otherfile")

            with temporary_dir() as static_resource_path:
                os.mkdir("TestBundle")
                touch("TestBundle/test.txt")
                touch("TestBundle.resource-meta.xml")

                task = create_task(
                    Deploy,
                    {
                        "path": path,
                        "static_resource_path": static_resource_path,
                        "namespace_tokenize": "ns",
                        "namespace_inject": "ns",
                        "namespace_strip": "ns",
                        "unmanaged": True,
                        "rest_deploy": rest_deploy,
                    },
                )

                api = task._get_api()
                zf = zipfile.ZipFile(io.BytesIO(base64.b64decode(api.package_zip)), "r")
                namelist = zf.namelist()
                assert "staticresources/TestBundle.resource" in namelist
                assert "staticresources/TestBundle.resource-meta.xml" in namelist
                package_xml = zf.read("package.xml").decode()
                assert "<name>StaticResource</name>" in package_xml
                assert "<members>TestBundle</members>" in package_xml
                zf.close()

    @pytest.mark.parametrize("rest_deploy", [True, False])
    def test_get_api__missing_path(self, rest_deploy):
        task = create_task(
            Deploy,
            {
                "path": "BOGUS",
                "unmanaged": True,
                "rest_deploy": rest_deploy,
            },
        )

        api = task._get_api()
        assert api is None

    @pytest.mark.parametrize("rest_deploy", [True, False])
    def test_get_api__empty_package_zip(self, rest_deploy):
        with temporary_dir() as path:
            task = create_task(
                Deploy,
                {
                    "path": path,
                    "unmanaged": True,
                    "rest_deploy": rest_deploy,
                },
            )

            api = task._get_api()
            assert api is None

    @pytest.mark.parametrize("rest_deploy", [True, False])
    def test_init_options(self, rest_deploy):
        with pytest.raises(TaskOptionsError):
            create_task(
                Deploy,
                {
                    "path": "empty",
                    "test_level": "RunSpecifiedTests",
                    "unmanaged": False,
                    "rest_deploy": rest_deploy,
                },
            )

        with pytest.raises(TaskOptionsError):
            create_task(
                Deploy, {"path": "empty", "test_level": "Test", "unmanaged": False}
            )

        with pytest.raises(TaskOptionsError):
            create_task(
                Deploy,
                {
                    "path": "empty",
                    "test_level": "RunLocalTests",
                    "specified_tests": ["TestA"],
                    "unmanaged": False,
                    "rest_deploy": rest_deploy,
                },
            )

    @pytest.mark.parametrize("rest_deploy", [True, False])
    def test_init_options__transforms(self, rest_deploy):
        d = create_task(
            Deploy,
            {
                "path": "src",
                "transforms": ["clean_meta_xml"],
                "rest_deploy": rest_deploy,
            },
        )

        assert len(d.transforms) == 1
        assert isinstance(d.transforms[0], CleanMetaXMLTransform)

    @pytest.mark.parametrize("rest_deploy", [True, False])
    def test_init_options__bad_transforms(self, rest_deploy):
        with pytest.raises(TaskOptionsError) as e:
            create_task(
                Deploy,
                {
                    "path": "src",
                    "transforms": [{}],
                    "rest_deploy": rest_deploy,
                },
            )

            assert "transform spec is not valid" in str(e)

    @pytest.mark.parametrize("rest_deploy", [True, False])
    def test_freeze_sets_kind(self, rest_deploy):
        task = create_task(
            Deploy,
            {
                "path": "path",
                "namespace_tokenize": "ns",
                "namespace_inject": "ns",
                "namespace_strip": "ns",
                "rest_deploy": rest_deploy,
            },
        )
        step = StepSpec(
            step_num=1,
            task_name="deploy",
            task_config=task.task_config,
            task_class=None,
            project_config=task.project_config,
        )

        assert all(s["kind"] == "metadata" for s in task.freeze(step))
