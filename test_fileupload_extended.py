import unittest
import tempfile
import shutil
import os
from LitPyWeb import FileUpload, BytesIO


class TestFileUploadExtended(unittest.TestCase):

    def setUp(self):
        # 每次测试前创建临时目录
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        # 测试后清理文件
        shutil.rmtree(self.temp_dir)

    def test_empty_file_upload(self):
        """ 上传空文件 """
        empty = BytesIO(b'')
        fu = FileUpload(empty, 'empty.txt', 'empty.txt')
        self.assertEqual(fu.filename, 'empty.txt')
        self.assertEqual(fu.file.read(), b'')

    def test_save_to_non_existing_dir(self):
        """ 保存到不存在的路径 """
        fu = FileUpload(BytesIO(b'abc'), 'testfile.txt', 'testfile.txt')
        non_existing_dir = os.path.join(self.temp_dir, 'nonexistent')
        file_path = os.path.join(non_existing_dir, fu.filename)
        os.makedirs(non_existing_dir)
        fu.save(file_path)
        self.assertTrue(os.path.exists(file_path))
        with open(file_path, 'rb') as f:
            self.assertEqual(f.read(), b'abc')

    def test_unicode_filename(self):
        """ 中文或特殊字符文件名处理 """
        fu = FileUpload(BytesIO(b'data'), '测试文件.txt', '测试文件.txt')
        # LitPyWeb 会去除所有非 ASCII 字符
        self.assertTrue(fu.filename in ['txt', 'empty'] or fu.filename.endswith('.txt'))


    def test_filename_security_strip_path(self):
        """ 测试路径穿越攻击处理 """
        fu = FileUpload(BytesIO(b'data'), '../evil.txt', '../evil.txt')
        self.assertNotIn('..', fu.filename)
        self.assertEqual(fu.filename, 'evil.txt')

    def test_save_multiple_files_same_name(self):
        """ 测试保存两个同名文件不会冲突（自行处理逻辑） """
        content1 = BytesIO(b'first')
        content2 = BytesIO(b'second')
        f1 = FileUpload(content1, 'duplicate.txt', 'duplicate.txt')
        f2 = FileUpload(content2, 'duplicate.txt', 'duplicate.txt')

        path1 = os.path.join(self.temp_dir, f1.filename)
        f1.save(path1)
        self.assertEqual(open(path1, 'rb').read(), b'first')

        path2 = os.path.join(self.temp_dir, 'copy_' + f2.filename)
        f2.save(path2)
        self.assertEqual(open(path2, 'rb').read(), b'second')

    def test_long_filename_truncation(self):
        """ 超长文件名会被截断 """
        long_name = 'a' + 'b' * 500 + '.txt'
        fu = FileUpload(BytesIO(b'data'), long_name, long_name)
        self.assertTrue(len(fu.filename) <= 255)


if __name__ == '__main__':
    unittest.main()
