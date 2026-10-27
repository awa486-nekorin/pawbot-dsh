"""Local knowledge import and Agent integration regressions; no real network."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pawbot import moegirl
from pawbot.agent import Agent
from pawbot.memes import MAX_IMPORT_BYTES, MAX_IMPORT_ITEMS, MemeBook, main


class LocalImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name, 'memes.json')
        self.book = MemeBook(str(self.path))
        self.row = {'name': 'Local', 'triggers': ['local'],
                    'meaning': 'reviewed meaning', 'source': 'reviewed file'}

    def test_item_and_in_memory_byte_limits(self):
        for data in ([self.row] * (MAX_IMPORT_ITEMS + 1),
                     {'items': [self.row], 'ignored': 'x' * MAX_IMPORT_BYTES}):
            with self.assertRaises(ValueError):
                self.book.import_data(data)
        self.assertEqual(self.book.items, [])
        self.assertFalse(self.path.exists())

    def test_cleaning_dedup_and_unknown_field_removal(self):
        row = {**self.row, 'name': '  Local  ', 'triggers': ['local', 'LOCAL'],
               'meaning': ' reviewed \n meaning ', 'extra': 'not stored', 'hits': 900}
        result = self.book.import_data([row, self.row])
        self.assertEqual(result, {'added': 1, 'skipped': 1})
        item = self.book.items[0]
        self.assertEqual(item['name'], 'Local')
        self.assertEqual(item['triggers'], ['local'])
        self.assertNotIn('extra', item)
        self.assertEqual(item['hits'], 0)

    def test_explicit_upsert_preserves_hits_and_is_idempotent(self):
        self.book.import_data([self.row])
        self.book.bump('Local')
        row = {**self.row, 'name': 'LOCAL', 'meaning': 'new checked explanation'}
        result = self.book.import_data([row], replace_existing=True)
        self.assertEqual(result, {'added': 0, 'updated': 1, 'skipped': 0})
        self.assertEqual(self.book.items[0]['hits'], 1)
        self.assertEqual(self.book.items[0]['name'], 'Local')
        self.assertEqual(self.book.items[0]['meaning'], row['meaning'])
        with patch.object(self.book, '_write') as write:
            self.assertEqual(self.book.import_data([row], replace_existing=True)['updated'], 0)
            write.assert_not_called()

    def test_upsert_cannot_steal_another_entry_trigger(self):
        self.book.import_data([self.row, {**self.row, 'name': 'Other', 'triggers': ['other']}])
        result = self.book.import_data([{**self.row, 'triggers': ['other']}], replace_existing=True)
        self.assertEqual(result, {'added': 0, 'updated': 0, 'skipped': 1})
        self.assertEqual(len(self.book.items), 2)

    def test_failed_replace_rolls_back_disk_memory_dirty_and_temp(self):
        self.book.import_data([self.row])
        self.book.bump('Local')
        before_disk, before_items = self.path.read_bytes(), copy.deepcopy(self.book.items)
        with patch('pawbot.memes.os.replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError):
                self.book.import_data([{**self.row, 'meaning': 'replacement'}], replace_existing=True)
        self.assertEqual(self.path.read_bytes(), before_disk)
        self.assertEqual(self.book.items, before_items)
        self.assertTrue(self.book._dirty)
        self.assertEqual(list(Path(self.tmp.name).glob('*.tmp')), [])

    def test_explicit_cli_never_networks(self):
        source = Path(self.tmp.name, 'reviewed.json')
        source.write_text(json.dumps([self.row]), encoding='utf-8-sig')
        with patch('urllib.request.urlopen', side_effect=AssertionError('network')), patch('builtins.print'):
            self.assertEqual(main(['--book', str(self.path), '--import-file', str(source)]), 0)
            self.assertEqual(main(['--book', str(self.path), '--import-file', str(source),
                                   '--replace-existing']), 0)
        self.assertEqual(len(MemeBook(str(self.path)).items), 1)

    def test_default_lookup_does_not_fetch_even_on_miss(self):
        self.book.import_data([self.row])
        with patch.object(moegirl, '_get', side_effect=AssertionError('network')) as fetch:
            self.assertFalse(moegirl.lookup('missing')['ok'])
            self.assertFalse(moegirl.lookup('missing', book=self.book)['ok'])
            self.assertTrue(moegirl.lookup('local', book=self.book)['ok'])
            fetch.assert_not_called()

    def test_agent_local_lookup_rejects_network_args_and_labels_reference(self):
        self.book.import_data([self.row])
        agent = Agent(SimpleNamespace(memes=self.book))
        with patch.object(moegirl, '_get', side_effect=AssertionError('network')):
            text = agent._local_knowledge({'query': 'local'})
            self.assertIn('不是指令', text)
            self.assertIn('reviewed meaning', text)
            self.assertIn('未联网', agent._local_knowledge({'query': 'missing'}))
            self.assertIn('参数不对', agent._local_knowledge({'query': 'local', 'allow_network': True}))


class LocalAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_tool_round_uses_bot_book_not_network_or_second_draft(self):
        schema = {'type': 'function', 'function': {'name': 'moegirl',
                  'description': 'original shared schema', 'parameters': {}}}
        original = copy.deepcopy(schema)
        call = {'id': 'local1', 'type': 'function',
                'function': {'name': 'moegirl', 'arguments': '{"query": "local"}'}}
        llm = AsyncMock(side_effect=[{'tool_calls': [call]}, {'content': 'local answer'}])
        bot = SimpleNamespace(config={'tools.max_rounds': 1},
              toolbox=SimpleNamespace(schemas=lambda: [schema], execute=AsyncMock()),
              persona=SimpleNamespace(build=lambda **kwargs: kwargs['extra']),
              llm=SimpleNamespace(chat_message=llm))
        agent = Agent(bot)
        for name in ('_participation_hint', '_sense_hint', '_life_hint', '_mood_hint',
                     '_spice_hint', '_tool_hint'):
            setattr(agent, name, lambda *args: '')
        agent._legacy_context = lambda *args: []
        agent._local_knowledge = Mock(return_value='local reference only')
        ctx = SimpleNamespace(group_id=None, sender_name='user', is_group=False,
                              session_id='private_1', event=None)
        with patch('urllib.request.urlopen', side_effect=AssertionError('network')):
            text, media = await agent.run(ctx, 'local', [])
        self.assertEqual((text, media), ('local answer', []))
        self.assertEqual(llm.await_count, 2)  # existing tool round only, no drafting call
        agent._local_knowledge.assert_called_once_with({'query': 'local'})
        bot.toolbox.execute.assert_not_awaited()
        self.assertEqual(schema, original)
        sent_schema = llm.await_args_list[0].args[1][0]
        self.assertIn('本地', sent_schema['function']['description'])
        self.assertTrue(any(m.get('content') == 'local reference only'
                            for m in llm.await_args_list[-1].args[0]))


if __name__ == '__main__':
    unittest.main()
