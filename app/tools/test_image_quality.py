"""图片过滤和完整文件下载测试；不连接外部服务。"""

import base64
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pawbot import imagesearch as images


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class ImageQualityTests(unittest.TestCase):
    def setUp(self):
        images._DL_CACHE.clear()          # 下载缓存是进程级的，别让上一条用例的结果漏过来

    def test_furry_is_excluded_for_all_ratings(self):
        for tag in ['福瑞', 'furry', 'ケモノ', 'kemono', '兽人', '獣人']:
            for rating in ['sfw', 'r18']:
                with self.subTest(tag=tag, rating=rating):
                    self.assertTrue(images.mentions_furry(tag))
                    self.assertFalse(images._allowed({'tags': tag, 'rating': rating}, True))
        self.assertFalse(images.mentions_furry('猫耳 狐耳 ケモミミ'))
        self.assertGreater(len(images._MINOR_TERMS), 20)





    def test_furry_query_does_not_call_sources(self):
        with patch.object(images, 'search_sogou') as search:
            result = images.image_search('福瑞 插画', {}, source='web')
        search.assert_not_called()
        self.assertEqual(result['images'], [])

    def test_no_byte_truncation_or_thumbnail_fallback(self):
        small = b'\xff\xd8\xff' + b'x' * 100
        with patch.object(images, 'download', return_value=small):
            result = images.to_segment({'url': 'https://example.com/a.jpg',
                                        'need_referer': True, 'rating': 'sfw'})
        self.assertEqual(base64.b64decode(result['data']['file'][9:]), small)
        # 大原图不再退回压缩版，完整写到本地缓存交给同机 NapCat
        big = b'\xff\xd8\xff' + b'x' * (images.MAX_INLINE + 10)
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(images, 'IMAGE_CACHE', Path(tmp)), \
                patch.object(images, 'download', return_value=big):
            result = images.to_segment({'url': 'https://example.com/a.jpg',
                                        'need_referer': True, 'rating': 'sfw'})
            uri = result['data']['file']
            self.assertTrue(uri.startswith('file:///'))
            path = Path(unquote(urlsplit(uri).path).lstrip('/'))
            self.assertEqual(path.read_bytes(), big)
        with patch.object(images, 'download', return_value=None):
            self.assertIsNone(images.to_segment({'url': 'https://example.com/a.jpg',
                                                'thumb': 'https://example.com/thumb.jpg',
                                                'need_referer': True, 'rating': 'sfw'}))

    def test_large_original_is_not_rejected_below_cap(self):
        original = b'\xff\xd8\xff' + b'x' * (8 * 1024 * 1024)
        with patch.object(images.urllib.request, 'urlopen', return_value=Response(original)):
            self.assertEqual(images.download({'url': 'https://example.com/original.jpg'}), original)

    def test_oversized_file_uses_real_alternative(self):
        good = b'\xff\xd8\xffgood'
        responses = [Response(b'\xff\xd8\xff' + b'x' * images.MAX_IMAGE_BYTES), Response(good)]
        item = {'url': 'https://example.com/original.jpg',
                'url_candidates': ['https://example.com/large.jpg'],
                'need_referer': True, 'referer': images.PIXIV_REFERER}
        with patch.object(images.urllib.request, 'urlopen', side_effect=responses) as opener:
            self.assertEqual(images.download(item), good)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(opener.call_args.args[0].get_header('Referer'), images.PIXIV_REFERER)


if __name__ == '__main__':
    unittest.main(verbosity=2)
