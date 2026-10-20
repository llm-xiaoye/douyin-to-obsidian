import argparse
import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/douyin-to-obsidian/scripts'))
import library
import note_assets
import pipeline

VID = '7652616197779915496'
ANALYSIS = {'title':'图记忆的机制', 'category':['AI','Agent记忆'], 'tags':['memory'], 'insight':'将关系和有效时间一起解释',
 'reviewed_pages':[1,2], 'learning_review':{'learning_goal':'能依据条件选择表示方式',
 'key_claims':[{'claim':'关系与有效时间应分别核验','evidence':'图文第 1 页','section':'机制'}, {'claim':'模型判断', 'evidence':'测试材料', 'section':'模型判断'}],
 'application':'将新旧任职记录代入查询', 'uncertainties':[]}}
BODY = '> 一句话结论：测试结论。\n\n## 核心要点\n\n- 一\n- 二\n- 三\n\n## 机制\n\n解释第一张图。\n\n{{DY_IMAGE:1}}\n\n## 应用\n\n解释第二张图。\n\n{{DY_IMAGE:2}}\n\n## 模型补充\n\n### 补充知识\n\n补充。\n\n### 纠错与边界\n\n边界。\n\n### 模型判断\n\n判断。\n\n## 自测\n\n题与答。\n'

class AssetTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name).resolve();self.vault=self.root/'大库'/'独立知识库';self.vault.mkdir(parents=True)
  self.cache=self.root/'cache';self.cache.mkdir()
  self.config={'vault':str(self.vault),'cache_dir':str(self.cache),'attachments_dir':'附件'}
  entries=[]
  for page in [1,2]:
   original=self.cache/f'{page}.webp';Image.new('RGB',(8,8),(page*50,20,30)).save(original,lossless=True)
   display=original.with_suffix('.png')
   with Image.open(original) as im:im.convert('RGB').save(display)
   entries.append({'page':page,'file':display.name,'sha256':note_assets.digest(display.read_bytes())})
  self.bundle={'aweme_id':VID,'source_url':f'https://www.douyin.com/note/{VID}','media_kind':'image','image_count':2,'images':entries}
  self.raw={'aweme_id':VID,'source_kind':'image','text':'图像知识','pages':[dict(e,text='测试知识') for e in entries]}
  pipeline.write_json(self.cache/'bundle.json',self.bundle);pipeline.write_json(self.cache/'transcript.json',self.raw)
  pipeline.write_json(self.cache/'analysis.json',ANALYSIS);(self.cache/'knowledge.md').write_text(BODY)
  self.args=argparse.Namespace(bundle=str(self.cache/'bundle.json'),summary=str(self.cache/'knowledge.md'),analysis=str(self.cache/'analysis.json'),vault=None,collection=None,archive_source=False)
 def save(self):
  with contextlib.redirect_stdout(io.StringIO()) as out:pipeline.save(self.args,self.config)
  return json.loads(out.getvalue())
 def files(self):
  return {p.relative_to(self.vault).as_posix():p.read_bytes() for p in self.vault.rglob('*') if p.is_file() and not p.name.startswith('.')}
 def targets(self,note):
  return [(note.parent/unquote(url)).resolve() for url in re.findall(r'!\[原图 \d+\]\(([^)]+)\)',note.read_text())]
 def test_original_images_inline_survive_cache_and_migration(self):
  r=self.save();note=Path(r['note']);targets=self.targets(note)
  self.assertEqual(len(targets),2)
  for page,p in enumerate(targets,1):
   self.assertTrue(p.is_relative_to(self.vault/'附件'));self.assertEqual(p.read_bytes(),(self.cache/f'{page}.webp').read_bytes())
  snapshot=self.files();self.assertEqual(self.save()['status'],'exists');self.assertEqual(snapshot,self.files())
  result=library.migrate(self.config,{VID:dict(ANALYSIS,category=['AI','知识管理','时间记忆'])})
  moved=self.vault/result['notes'][0];self.assertEqual(self.targets(moved),targets)
  for p in list(self.cache.glob('*.webp'))+list(self.cache.glob('*.png')):p.unlink()
  self.assertTrue(all(p.is_file() for p in self.targets(moved)))
 def test_missing_duplicate_fenced_or_unknown_markers_do_not_write(self):
  for body in [BODY.replace('{{DY_IMAGE:2}}',''),BODY+'\n{{DY_IMAGE:1}}',BODY.replace('{{DY_IMAGE:1}}','```\n{{DY_IMAGE:1}}\n```'),BODY.replace('{{DY_IMAGE:2}}','{{DY_IMAGE:3}}')]:
   (self.cache/'knowledge.md').write_text(body)
   with self.assertRaises(ValueError):self.save()
   self.assertEqual(self.files(),{})
 def test_metadata_failure_rolls_back_images_and_note(self):
  original=library.atomic_write
  def broken(path,content,exclusive=False):
   if path.name=='知识索引.md':raise OSError('disk failure')
   return original(path,content,exclusive)
  with patch.object(library,'atomic_write',side_effect=broken):
   with self.assertRaises(OSError):self.save()
  self.assertEqual(self.files(),{})
  self.assertFalse((self.vault/'.dy-transaction.json').exists())
 def test_attachment_collision_is_preserved(self):
  images=note_assets.evidence_images(self.bundle,self.cache)
  note=library.new_path(self.vault,self.config,library.validate_analysis(ANALYSIS))
  _,changes,_=note_assets.stage(self.vault,self.config,note,BODY,images)
  dest=next(iter(changes));dest.parent.mkdir(parents=True);dest.write_bytes(b'user modified image')
  with self.assertRaises(ValueError):self.save()
  self.assertEqual(dest.read_bytes(),b'user modified image');self.assertFalse(note.exists())
 def test_explicit_revision_backs_up_and_preserves_personal_content(self):
  first=self.save();note=Path(first['note']);note.write_text(note.read_text()+'\n我的亲自验证结果。\n### 来源\n我的个人链接。\n')
  before=note.read_bytes();created=library.read_note(note)[0]['created']
  (self.cache/'knowledge.md').write_text(BODY.replace('解释第一张图','深入解释并列出适用前提'))
  self.args.command='revise';self.args.expected_note_sha256=note_assets.digest(before)
  self.args.expected_evidence_sha256=note_assets.digest((self.cache/'transcript.json').read_bytes())
  result=self.save();self.assertEqual(result['status'],'revised')
  self.assertIn('我的亲自验证结果。\n### 来源\n我的个人链接。',note.read_text())
  self.assertIn('深入解释并列出适用前提',note.read_text());self.assertEqual(library.read_note(note)[0]['created'],created)
  backup=json.loads((Path(result['backup'])/'before.json').read_text())
  self.assertEqual(base64.b64decode(backup[note.relative_to(self.vault).as_posix()]),before)
  snapshot=self.files()
  with self.assertRaises(ValueError):self.save()
  self.assertEqual(snapshot,self.files())
 def test_revision_rejects_changed_evidence_and_absent_review(self):
  first=self.save();self.args.command='revise';self.args.expected_note_sha256=note_assets.digest(Path(first['note']).read_bytes());self.args.expected_evidence_sha256='old'
  snapshot=self.files()
  with self.assertRaises(ValueError):self.save()
  self.assertEqual(snapshot,self.files())
  pipeline.write_json(self.cache/'analysis.json',{k:v for k,v in ANALYSIS.items() if k!='learning_review'})
  with self.assertRaises(ValueError):self.save()
  self.assertEqual(snapshot,self.files())

if __name__=='__main__':unittest.main()
