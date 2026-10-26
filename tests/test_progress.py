import unittest

from segio_archive.progress import ProgressState, Reporter, duration


class ProgressTests(unittest.TestCase):
    def event(self,stage,completed=None,total=None,unit='',detail=''):
        return dict(stage=stage,completed=completed,total=total,unit=unit,detail=detail)

    def test_elapsed_time_runs_without_worker_events_and_freezes_on_cancel(self):
        clock=[10.0]
        state=ProgressState(clock=lambda:clock[0])
        state.begin()
        self.assertIsNone(state.view()['current'])
        clock[0]=70
        state.update(self.event('transfer',5*1024**3,10*1024**3,'bytes'))
        clock[0]=75
        view=state.view()
        self.assertEqual(view['current'],50)
        self.assertEqual(view['overall'],30)
        self.assertIn('5.00 GiB / 10.00 GiB',view['detail'])
        self.assertEqual(view['elapsed'],'전체 경과 00:01:05 · 현재 단계 00:00:05')
        state.finish(False,cancelled=True)
        clock[0]=10000
        state.update(self.event('cleanup',1,1,'steps'))
        self.assertEqual(state.view()['elapsed'],view['elapsed'])
        self.assertIn('중단',state.view()['stage'])
        self.assertEqual(state.view()['overall'],30)

    def test_step_bar_only_finishes_on_success_and_new_jobs_reset_time(self):
        clock=[0]
        state=ProgressState(clock=lambda:clock[0])
        state.begin(make_zip=True)
        self.assertIn('zip',state.stages)
        self.assertEqual(len(state.stages),11)
        state.update(self.event('cleanup',1,1,'steps'))
        self.assertLess(state.view()['overall'],100)
        state.finish(False)
        self.assertLess(state.view()['overall'],100)
        state.finish(True)
        self.assertEqual(state.view()['overall'],100)
        self.assertEqual(state.view()['current'],100)
        clock[0]=100
        state.begin('update')
        self.assertEqual(state.stages,['validate','mail','index'])
        self.assertEqual(state.view()['overall'],0)
        self.assertIn('00:00:00',state.view()['elapsed'])
        state.update(self.event('mail',0,0,'messages'))
        self.assertEqual(state.view()['current'],100)
        self.assertLess(state.view()['overall'],100)
        state.begin('check')
        self.assertEqual(state.stages,['connect','environment'])

    def test_frequent_block_reports_are_coalesced_but_stage_changes_and_completion_arrive(self):
        clock=[0]
        events=[]
        report=Reporter(events.append,clock=lambda:clock[0])
        for count in range(10001):
            report('transfer',count,10000,'bytes')
        self.assertEqual(len(events),2)
        self.assertEqual(events[-1]['completed'],10000)
        report('mail',0,100,'messages')
        self.assertEqual(events[-1]['stage'],'mail')
        clock[0]=1
        report('mail',40,100,'messages')
        self.assertEqual(events[-1]['completed'],40)
        report('index',-1,'invalid','records')
        self.assertIsNone(events[-1]['completed'])
        self.assertIsNone(events[-1]['total'])

    def test_long_jobs_keep_hours_without_wrapping_at_one_day(self):
        self.assertEqual(duration(3*24*3600+61),'72:01:01')
        self.assertEqual(duration(-1),'00:00:00')
