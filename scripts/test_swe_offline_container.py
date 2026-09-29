import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import contextlib
import io
import sys
import tempfile

spec=importlib.util.spec_from_file_location('offline',Path(__file__).with_name('swe_offline_container.py'))
offline=importlib.util.module_from_spec(spec)
spec.loader.exec_module(offline)


class OfflineContainerTests(unittest.TestCase):
    def test_offline_creation_and_inspection(self):
        client=Mock();container=client.containers.create.return_value
        container.attrs={'HostConfig':{'NetworkMode':'none'},'NetworkSettings':{'Networks':{'none':{}}}}
        result=offline.create_offline_container(Mock(image='local',instance_id='task'),client,'run',Mock())
        self.assertIs(result,container)
        client.images.get.assert_called_once_with('local')
        self.assertEqual(client.containers.create.call_args.kwargs['network_mode'],'none')
        container.reload.assert_called_once()

    def test_unexpected_network_is_rejected_and_container_removed(self):
        for attrs in [{}, {'HostConfig':{'NetworkMode':'bridge'}},
                      {'HostConfig':{'NetworkMode':'none'},'NetworkSettings':{'Networks':{'bridge':{}}}}]:
            client=Mock();container=client.containers.create.return_value;container.attrs=attrs
            with self.subTest(attrs=attrs),self.assertRaises(RuntimeError):
                offline.create_offline_container(Mock(image='local',instance_id='task'),client,'run',Mock())
            container.remove.assert_called_once_with(force=True)

    def test_missing_image_does_not_create_or_pull(self):
        client=Mock();client.images.get.side_effect=RuntimeError('missing image')
        with self.assertRaises(RuntimeError):
            offline.create_offline_container(Mock(image='local',instance_id='task'),client,'run',Mock())
        client.images.pull.assert_not_called();client.containers.create.assert_not_called()

    def test_eval_dry_run_finds_case_sensitive_model_directories(self):
        for benchmark, entry, domain, iid in [
            ('verified','run_swebench_docker_eval.py','django','django__django-1'),
            ('pro','run_swebench_pro_eval.py','ansible','instance_ansible__ansible-1')]:
            with self.subTest(benchmark=benchmark),tempfile.TemporaryDirectory() as tmp:
                module_spec=importlib.util.spec_from_file_location('eval_'+benchmark,Path(__file__).with_name(entry))
                module=importlib.util.module_from_spec(module_spec);module_spec.loader.exec_module(module)
                root=Path(tmp);model='chatdev_CaseSensitive'
                folder=root/f'chatdev_{domain}'/'output_selected'/('swe_bench_'+benchmark)/model
                folder.mkdir(parents=True);(folder/(iid+'.txt')).write_text('diff --git a/example b/example\n')
                argv=[entry,'--domain',domain,'--output-root',str(root),'--model-name',model,
                      '--report-dir',str(root/'reports'),'--instance-ids',iid,'--dry-run']
                output=io.StringIO()
                with patch.object(sys,'argv',argv),contextlib.redirect_stdout(output):
                    if benchmark=='verified':
                        with patch.object(module,'write_predictions',return_value=root/'predictions.json') as save:
                            module.main()
                            self.assertEqual(save.call_args.args[0][0]['instance_id'],iid)
                    else:
                        with patch.object(module,'load_specs',return_value={iid:{}}),patch.object(module,'docker') as docker:
                            module.main();docker.assert_not_called()
                self.assertNotIn('no patch dir',output.getvalue())
                self.assertNotIn('no predictions',output.getvalue())
                self.assertIn('would eval' if benchmark=='pro' else 'dry-run',output.getvalue())


if __name__=='__main__':unittest.main()
