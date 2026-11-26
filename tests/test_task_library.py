from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from packages.ground_core.task_library import TaskLibrary, TaskLibraryConflict


class TaskLibraryTests(unittest.TestCase):
    def test_save_and_reopen_one_hundred_points(self):
        with TemporaryDirectory() as temp:
            path = Path(temp)/'task-library.json'
            library = TaskLibrary(path, 'run')
            points = [dict(kind='point', id=str(uuid4()), label=f'任务点{i}', east=i, north=20)
                      for i in range(1, 101)]
            saved = library.save(dict(library.read(), tasks=points))
            with self.assertRaisesRegex(ValueError, '100'):
                library.save(dict(saved, tasks=points+[dict(points[0], id=str(uuid4()), label='任务点101')]))
            self.assertEqual(TaskLibrary(path, 'run').read()['tasks'], points)

    def test_replace_reopen_and_isolate(self):
        with TemporaryDirectory() as temp:
            root=Path(temp)
            path=root/'run-a'/'task-library.json'
            a=TaskLibrary(path,'run-a')
            point=dict(kind='point',id=str(uuid4()),label='任务点1',east=10,north=20)
            region=dict(kind='area',id=str(uuid4()),label='河堤东段',west=0,east=90,south=-25,north=80)
            initial=a.read()
            self.assertEqual(a.save(dict(initial,tasks=[point,region]))['revision'],1)
            a=TaskLibrary(path,'run-a')
            saved=a.read()
            self.assertEqual(saved['tasks'],[point,region])
            updated=a.save(dict(saved,tasks=[region]))
            self.assertEqual(a.read('run-a',2)['tasks'],[region])
            with self.assertRaises(TaskLibraryConflict):
                a.save(dict(saved,tasks=[point]))
            with self.assertRaises(TaskLibraryConflict):
                a.read('run-a',1)
            self.assertEqual(a.save(dict(updated,tasks=[]))['tasks'],[])
            b=TaskLibrary(root/'run-b'/'task-library.json','run-b')
            self.assertEqual(b.read()['revision'],0)
            with self.assertRaises(TaskLibraryConflict):
                b.save(initial)

    def test_invalid_snapshot_leaves_library_intact(self):
        with TemporaryDirectory() as temp:
            library=TaskLibrary(Path(temp)/'task-library.json','run')
            draft=library.read()
            point=dict(kind='point',id=str(uuid4()),label='重名',east=10,north=20)
            for tasks in ([point,dict(point,id=str(uuid4()))],
                          [point,dict(kind='area',id=str(uuid4()),label='逆序',west=80,east=0,south=0,north=20)]):
                with self.assertRaises(ValueError):
                    library.save(dict(draft,tasks=tasks))
            self.assertEqual(library.read(),draft)

class ReplayTests(unittest.TestCase):
    def test_concurrent_lanes_finish_independently_and_confirm_is_idempotent(self):
        from unittest.mock import patch
        from packages.ground_core.library_replay import LibraryReplay
        plan = dict(id=uuid4().hex, unassigned={}, assignments={'uav1':['p'], 'uav2':['r']},
                    tasks=[dict(id='p',label='点',east_m=4,north_m=5),dict(id='r',label='区',
                    east_m=9,north_m=10,area={'west':0,'south':0,'east':18,'north':20})])
        with TemporaryDirectory() as tmp, patch('packages.ground_core.library_replay.time.time') as clock:
            replay=LibraryReplay(Path(tmp))
            clock.return_value=100
            state=replay.start(plan)
            self.assertEqual(state['lanes']['uav2']['current'],'r')
            clock.return_value=146
            state=replay.start(plan)
            self.assertEqual(state['lanes']['uav1']['phase'],'finished')
            self.assertEqual(state['tasks']['r']['status'],'running')
            clock.return_value=191
            self.assertEqual(LibraryReplay(Path(tmp)).status(plan['id'])['tasks']['r']['status'],'completed')
