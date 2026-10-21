import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/douyin-to-obsidian/scripts'))
import auto_route
import dy


class AutoRouteTests(unittest.TestCase):
    def hook(self, text, event='UserPromptSubmit'):
        return subprocess.run([sys.executable, str(Path(auto_route.__file__))],
                              input=json.dumps({'hook_event_name':event,'prompt':text},ensure_ascii=False),
                              text=True,capture_output=True,timeout=5)

    def test_video_and_image_shares_without_slash_are_routed(self):
        for text in ['7.43 复制打开抖音，看看【作者的作品】https://v.douyin.com/soOFh45DdUQ/ yTl:/',
                     '4.35 复制打开抖音，看看【图文作品】https://v.douyin.com/dsEdBl-cXRI/']:
            result=self.hook(text)
            self.assertEqual(result.returncode,0)
            data=json.loads(result.stdout)
            self.assertEqual(data['hookSpecificOutput']['hookEventName'],'UserPromptSubmit')
            self.assertTrue(data['suppressOutput'])
            self.assertIn(str(auto_route.SKILL_DIR/'SKILL.md'),data['hookSpecificOutput']['additionalContext'])
            self.assertEqual(dy.parse_input(text)[0],'videos')

    def test_nonmatching_and_explicit_commands_are_unchanged(self):
        for text in ['今天学习什么','https://v.douyin.com/dsEdBl-cXRI/',
                     '/dy 复制打开抖音 https://v.douyin.com/dsEdBl-cXRI/',
                     '/other-command 复制打开抖音 https://v.douyin.com/dsEdBl-cXRI/']:
            self.assertEqual(self.hook(text).stdout,'')
        self.assertEqual(self.hook('复制打开抖音','PostToolUse').stdout,'')

    def test_expanded_dy_is_not_routed_twice(self):
        template=(auto_route.SKILL_DIR/'integrations/cc-connect/dy.md').read_text()
        self.assertEqual(self.hook(template+'\n复制打开抖音 https://v.douyin.com/dsEdBl-cXRI/').stdout,'')

    def test_missing_link_never_becomes_a_folder_request(self):
        self.assertTrue(self.hook('复制打开抖音，看看这个作品').stdout)
        with self.assertRaisesRegex(ValueError,'没有找到完整链接'):
            dy.parse_input('复制打开抖音，看看这个作品')

    def test_batch_preserves_ids_and_deduplicates_links(self):
        raw='复制打开抖音 https://www.douyin.com/video/7632267180838522162\n复制打开抖音 https://www.douyin.com/note/7685002150695911409\n复制打开抖音 https://www.douyin.com/note/7685002150695911409'
        mode, links=dy.parse_input(raw)
        self.assertEqual(mode,'videos')
        self.assertEqual(len(links),2)
        self.assertTrue(links[1].endswith('7685002150695911409'))

    def test_hook_never_echoes_or_executes_source_content(self):
        text='复制打开抖音 $(touch /tmp/dy-router-should-not-run) `unknown-command` https://v.douyin.com/test/'
        result=self.hook(text)
        self.assertEqual(result.returncode,0)
        self.assertNotIn('touch',result.stdout)
        self.assertNotIn('unknown-command',result.stdout)
        self.assertEqual(result.stderr,'')
        result=subprocess.run([sys.executable,auto_route.__file__],input='not json',text=True,capture_output=True)
        self.assertEqual((result.returncode,result.stdout,result.stderr),(0,'',''))


if __name__=='__main__':
    unittest.main()
