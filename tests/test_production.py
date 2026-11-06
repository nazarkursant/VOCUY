import unittest
import production


class TestCsvInput(unittest.TestCase):
    def test_custom_columns_inspect_and_keep_left_to_right_order(self):
        source = 'video_id,intro,narration,outro\nONE,Hello,Story one,Bye\nTWO,Hello,Story two,Bye'
        self.assertEqual(production.inspect_csv(source), {'columns': [
            {'id': name, 'label': name} for name in ('intro', 'narration', 'outro')], 'videos': 2})
        clips = production.parse_csv(source, static_columns=['intro', 'outro'])
        self.assertEqual([c['segment_id'] for c in clips], ['intro', 'narration', 'outro'] * 2)
        self.assertEqual([c.get('static_key') for c in clips], ['intro', None, 'outro'] * 2)
        self.assertEqual(production.recording_count(clips), 4)
        self.assertEqual(production.recording_count(production.parse_csv(source)), 6)

    def test_static_text_may_be_entered_once_and_blank_cells_are_filled(self):
        source = 'video_id,intro,narration\nONE,Hello,First\nTWO,,Second'
        clips = production.parse_csv(source, static_columns=['intro'])
        self.assertEqual(clips[0]['text'], clips[2]['text'])
        with self.assertRaises(ValueError):
            production.parse_csv(source)

    def test_static_validation_rejects_conflicts_unknown_choices_and_partial_sharing(self):
        for source, columns in (
            ('video_id,intro\nONE,Hello\nTWO,Goodbye', ['intro']),
            ('video_id,intro\nONE,\nTWO,', ['intro']),
            ('video_id,intro\nONE,Hello', ['missing']),
            ('video_id,intro\nONE,Hello', 'intro'),
        ):
            with self.subTest(source=source, columns=columns), self.assertRaises(ValueError):
                production.parse_csv(source, static_columns=columns)
        clips = production.parse_csv('video_id,intro\nONE,Hello\nTWO,Hello', static_columns=['intro'])
        del clips[1]['static_key']
        with self.assertRaises(ValueError):
            production.validate_clips(clips)

    def test_static_selection_works_with_segment_and_topics_formats(self):
        narrow = 'video_id,segment_id,text\nONE,intro,Hello\nTWO,intro,'
        self.assertEqual(production.recording_count(production.parse_csv(narrow, static_columns=['intro'])), 1)
        wide = 'video_id,topic,what_is_it,how_to_use_it,cake_explanation\nONE,Title,Definition,Usage,Cake\nTWO,Title two,Definition,Usage two,Cake two'
        self.assertIn({'id': 'what', 'label': 'what_is_it'}, production.inspect_csv(wide)['columns'])
        self.assertEqual(production.recording_count(production.parse_csv(wide, static_columns=['what'])), 7)

    def test_three_example_files_have_five_sequences_and_expected_shared_counts(self):
        from pathlib import Path
        for name, keys, count in [('intro', ['intro'], 6), ('outro', ['outro'], 6), ('intro_outro', ['intro', 'outro'], 7)]:
            source = (Path(__file__).resolve().parent.parent / 'examples' / f'batch_shared_{name}.csv').read_text()
            with self.subTest(name=name):
                self.assertEqual(production.inspect_csv(source)['videos'], 5)
                self.assertEqual(production.recording_count(production.parse_csv(source, static_columns=keys)), count)

    def test_topics_expand_in_segment_order_and_preserve_quoted_text(self):
        source = '\ufeffvideo_id,topic,what_is_it,how_to_use_it,cake_explanation\r\nVIDEO_001,Title,"Hello, world","First line\nSecond line",Cake\r\n'
        clips = production.parse_csv(source)
        self.assertEqual([c['segment_id'] for c in clips], ['topic', 'what', 'usage', 'cake'])
        self.assertEqual(clips[1]['text'], 'Hello, world')
        self.assertEqual(clips[2]['text'], 'First line\nSecond line')

    def test_segment_input_keeps_ids_and_revisions(self):
        clips = production.parse_csv('video_id,segment_id,text,script_revision\nVIDEO_001,what,Hello,r2\nVIDEO_002,topic,Title,r1')
        self.assertEqual([(c['video_id'], c['segment_id']) for c in clips], [('VIDEO_001', 'what'), ('VIDEO_002', 'topic')])
        self.assertEqual(clips[0]['script_revision'], 'r2')

    def test_cake_hook_is_only_removed_when_explicitly_selected(self):
        source = 'video_id,segment_id,text\nVIDEO_001,cake,Let\'s explain it with cake. Cake body'
        self.assertTrue(production.parse_csv(source)[0]['text'].startswith("Let's"))
        self.assertEqual(production.parse_csv(source, True)[0]['text'], 'Cake body')

    def test_rejects_bad_columns_duplicates_empty_text_and_path_traversal(self):
        for source in (
            'id,text\n1,Hello',
            'video_id,segment_id,text\nVIDEO_001,what,Hello\nvideo_001,what,Again',
            'video_id,segment_id,text\n../outside,topic,Hello',
            'video_id,segment_id,text\nVIDEO_001,../topic,Hello',
            'video_id,segment_id,text\nCON,topic,Hello',
            'video_id,segment_id,text\nVIDEO_001,topic,',
            'video_id,segment_id,text\nVIDEO_001,topic,Unquoted,comma',
            'video_id,segment_id,text\nVIDEO_001,topic,"Unclosed quote',
            'video_id,segment_id,text,text\nVIDEO_001,topic,Hello,Hi',
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                production.parse_csv(source)

    def test_accepts_reference_scale_of_100_videos_and_400_clips(self):
        source = 'video_id,topic,what_is_it,how_to_use_it,cake_explanation\n'
        source += '\n'.join(f'VIDEO_{i:03d},Topic,Definition,Usage,Cake' for i in range(1, 101))
        clips = production.parse_csv(source)
        self.assertEqual(len(clips), 400)
        self.assertEqual(clips[-1]['number'], 400)

    def test_limits_apply_before_generation(self):
        with self.assertRaises(ValueError):
            production.validate_clips([{'video_id':'VIDEO_001','segment_id':'topic','text':'x'*10001}])
        with self.assertRaises(ValueError):
            production.validate_clips([{'video_id':f'VIDEO_{i}','segment_id':'topic','text':'x'*10000} for i in range(51)])
