"""
IMAP邮件处理模块 - 增强版
提供更丰富的日志记录、错误检测和进度通知
"""

import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
import os
import logging
from datetime import datetime
import threading
import socket
import ssl
import time
import traceback
from typing import List, Dict, Optional, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
import queue
import json

from .common import (
    decode_mime_words,
    parse_email_date,
    decode_email_content,
    parse_email_message,
    extract_email_content,
    normalize_check_time,
    format_date_for_imap_search
)
from .logger import (
    logger,
    log_email_start,
    log_email_complete,
    log_email_error,
    log_message_processing,
    log_message_error,
    log_progress,
    timing_decorator
)

logger = logging.getLogger(__name__)

class IMAPMailHandler:
    """IMAP邮箱处理类 - 增强版"""

    # 常用文件夹映射
    DEFAULT_FOLDERS = {
        'INBOX': ['INBOX', 'Inbox', 'inbox'],
        'SENT': ['Sent', 'SENT', 'Sent Items', 'Sent Messages', '已发送'],
        'DRAFTS': ['Drafts', 'DRAFTS', 'Draft', '草稿箱'],
        'TRASH': ['Trash', 'TRASH', 'Deleted', 'Deleted Items', 'Deleted Messages', '垃圾箱', '已删除'],
        'SPAM': ['Spam', 'SPAM', 'Junk', 'Junk E-mail', 'Bulk Mail', '垃圾邮件'],
        'ARCHIVE': ['Archive', 'ARCHIVE', 'All Mail', '归档']
    }

    def __init__(self, server, username, password, use_ssl=True, port=None):
        """初始化IMAP处理器"""
        self.server = server
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.port = port or (993 if use_ssl else 143)
        self.mail = None
        self.error = None

        # 自动检测服务器
        if not server and '@' in username:
            domain = username.split('@')[1].lower()
            if 'gmail' in domain:
                self.server = 'imap.gmail.com'
            elif 'qq.com' in domain:
                self.server = 'imap.qq.com'
            elif 'outlook' in domain or 'hotmail' in domain or 'live' in domain:
                self.server = 'outlook.office365.com'
            elif '163.com' in domain:
                self.server = 'imap.163.com'
            elif '126.com' in domain:
                self.server = 'imap.126.com'

    def connect(self):
        """连接到IMAP服务器"""
        try:
            if self.use_ssl:
                self.mail = imaplib.IMAP4_SSL(self.server, self.port)
            else:
                self.mail = imaplib.IMAP4(self.server, self.port)

            self.mail.login(self.username, self.password)
            return True
        except Exception as e:
            self.error = str(e)
            logger.error(f"IMAP连接失败: {e}")
            return False

    def get_folders(self):
        """获取文件夹列表"""
        if not self.mail:
            return []

        try:
            _, folders = self.mail.list()
            folder_list = []

            for folder in folders:
                if isinstance(folder, bytes):
                    folder = folder.decode('utf-8', errors='ignore')

                # 解析文件夹名称
                parts = folder.split('"')
                if len(parts) >= 3:
                    folder_name = parts[-2]
                else:
                    # 简单解析
                    folder_name = folder.split()[-1]

                if folder_name and folder_name not in ['.', '..']:
                    folder_list.append(folder_name)

            # 确保常用文件夹在列表中
            default_folders = ['INBOX', 'Sent', 'Drafts', 'Trash', 'Spam']
            for df in default_folders:
                if df not in folder_list:
                    folder_list.append(df)

            return sorted(folder_list)
        except Exception as e:
            logger.error(f"获取文件夹列表失败: {e}")
            return ['INBOX']

    def get_messages(self, folder="INBOX", limit=50):
        """获取指定文件夹的邮件"""
        if not self.mail:
            return []

        try:
            self.mail.select(folder)
            _, messages = self.mail.search(None, 'ALL')
            message_numbers = messages[0].split()

            # 限制数量并倒序（最新的在前）
            message_numbers = message_numbers[-limit:] if len(message_numbers) > limit else message_numbers
            message_numbers.reverse()

            mail_list = []
            for num in message_numbers:
                try:
                    _, msg_data = self.mail.fetch(num, '(RFC822)')
                    email_body = msg_data[0][1]
                    msg = email.message_from_bytes(email_body)

                    mail_record = parse_email_message(msg, folder)
                    if mail_record:
                        mail_list.append(mail_record)
                except Exception as e:
                    logger.warning(f"解析邮件失败: {e}")
                    continue

            return mail_list
        except Exception as e:
            logger.error(f"获取邮件失败: {e}")
            return []

    def close(self):
        """关闭连接"""
        if self.mail:
            try:
                self.mail.close()
                self.mail.logout()
            except:
                pass
            self.mail = None

    @staticmethod
    def _process_email_batch(email_address, password, server, port, use_ssl, folder, message_numbers, search_criteria, progress_queue, result_queue, thread_id):
        """处理一批邮件的工作函数"""
        mail = None
        processed_emails = []
        
        try:
            # 为每个线程创建独立的IMAP连接
            if use_ssl:
                mail = imaplib.IMAP4_SSL(server, port)
            else:
                mail = imaplib.IMAP4(server, port)
            
            mail.login(email_address, password)
            mail.select(folder)
            
            # 处理分配给这个线程的邮件
            for i, num in enumerate(message_numbers):
                try:
                    # 发送进度更新
                    progress_queue.put(('progress', thread_id, i + 1, len(message_numbers)))
                    
                    # 获取邮件头信息，用于提前判断是否已存在
                    _, header_data = mail.fetch(num, '(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])')
                    msg_header = email.message_from_bytes(header_data[0][1])

                    subject = decode_mime_words(msg_header.get("subject", "")) if msg_header.get("subject") else "(无主题)"
                    sender = decode_mime_words(msg_header.get("from", "")) if msg_header.get("from") else "(未知发件人)"
                    date_str = msg_header.get("date", "")
                    received_time = parse_email_date(date_str) if date_str else datetime.now()

                    # 创建一个唯一标识用于检查邮件是否已存在
                    mail_key = f"{subject}|{sender}|{received_time.isoformat()}"

                    # 获取完整邮件内容
                    _, msg_data = mail.fetch(num, '(RFC822)')
                    email_body = msg_data[0][1]

                    # 尝试使用标准方式解析邮件
                    try:
                        msg = email.message_from_bytes(email_body)
                        mail_record = parse_email_message(msg, folder)
                    except Exception as e:
                        logger.warning(f"Thread {thread_id}: 标准方式解析邮件失败，尝试使用EML解析器: {str(e)}")
                        mail_record = None

                    # 如果标准解析失败，尝试使用EML解析器
                    if not mail_record:
                        try:
                            from .file_parser import EmailFileParser
                            logger.info(f"Thread {thread_id}: 使用EML解析器解析邮件")
                            mail_record = EmailFileParser.parse_eml_content(email_body)
                            if mail_record:
                                # 设置文件夹信息
                                mail_record['folder'] = folder
                        except Exception as e:
                            logger.error(f"Thread {thread_id}: EML解析器解析邮件失败: {str(e)}")
                            mail_record = None

                    if mail_record:
                        # 添加一些额外信息用于去重判断
                        mail_record['mail_key'] = mail_key
                        processed_emails.append(mail_record)
                        message_id = mail_record.get('message_id', 'unknown')
                        subject = mail_record.get('subject', '(无主题)')
                        logger.debug(f"Thread {thread_id}: 成功处理邮件 {subject[:30]}")
                    else:
                        logger.error(f"Thread {thread_id}: 无法解析邮件: {mail_key}")

                except Exception as e:
                    logger.error(f"Thread {thread_id}: 处理邮件失败: {str(e)}")
                    continue
            
            # 发送完成结果
            result_queue.put(('success', thread_id, processed_emails))
            
        except Exception as e:
            logger.error(f"Thread {thread_id}: 线程处理失败: {str(e)}")
            result_queue.put(('error', thread_id, str(e)))
        finally:
            if mail:
                try:
                    mail.close()
                    mail.logout()
                except:
                    pass

    @staticmethod
    @timing_decorator
    def fetch_emails(email_address, password, server, port=993, use_ssl=True, folder="INBOX", callback=None, last_check_time=None):
        """获取邮箱中的邮件 - 多线程版本"""
        mail_records = []
        mail = None

        try:
            # 创建回调函数
            if callback is None:
                callback = lambda progress, message: None

            # 标准化处理last_check_time
            last_check_time = normalize_check_time(last_check_time)

            if last_check_time:
                logger.info(f"获取自 {last_check_time.isoformat()} 以来的新邮件")
            else:
                logger.info(f"获取所有邮件")

            # 连接IMAP服务器获取邮件列表
            logger.info(f"连接IMAP服务器 {server}:{port} (SSL: {use_ssl})")
            if callback:
                callback(0, "正在连接邮箱服务器")

            if use_ssl:
                mail = imaplib.IMAP4_SSL(server, port)
            else:
                mail = imaplib.IMAP4(server, port)

            # 登录
            logger.info(f"登录邮箱 {email_address}")
            if callback:
                callback(10, "正在登录邮箱")

            mail.login(email_address, password)

            # 选择邮件文件夹
            logger.info(f"选择文件夹 {folder}")
            if callback:
                callback(20, f"正在选择文件夹 {folder}")

            mail.select(folder)

            # 搜索邮件
            search_criteria = 'ALL'

            # 如果提供了上次检查时间，只获取新邮件
            if last_check_time:
                # 将日期转换成IMAP搜索格式 (DD-MMM-YYYY)
                date_str = format_date_for_imap_search(last_check_time)
                if date_str:
                    search_criteria = f'SINCE {date_str}'
                    logger.info(f"获取自 {date_str} 以来的新邮件")

            _, messages = mail.search(None, search_criteria)
            message_numbers = messages[0].split()
            total_messages = len(message_numbers)

            logger.info(f"找到 {total_messages} 封邮件")

            # 关闭初始连接，让线程使用独立连接
            mail.close()
            mail.logout()
            mail = None

            if total_messages == 0:
                if callback:
                    callback(100, "没有找到邮件")
                return mail_records

            # 将邮件分配给8个线程
            num_threads = min(8, total_messages)  # 如果邮件数少于8个，使用实际邮件数
            chunk_size = max(1, total_messages // num_threads)
            
            # 分割邮件列表
            message_chunks = []
            for i in range(0, total_messages, chunk_size):
                chunk = message_numbers[i:i + chunk_size]
                if chunk:
                    message_chunks.append(chunk)
            
            # 如果分割后的chunks数量超过8个，合并最后的chunks
            if len(message_chunks) > num_threads:
                last_chunks = message_chunks[num_threads-1:]
                merged_chunk = []
                for chunk in last_chunks:
                    merged_chunk.extend(chunk)
                message_chunks = message_chunks[:num_threads-1] + [merged_chunk]

            logger.info(f"使用 {len(message_chunks)} 个线程并行处理邮件")

            # 创建队列用于线程间通信
            progress_queue = queue.Queue()
            result_queue = queue.Queue()

            # 启动线程
            threads = []
            for i, chunk in enumerate(message_chunks):
                thread = threading.Thread(
                    target=IMAPMailHandler._process_email_batch,
                    args=(email_address, password, server, port, use_ssl, folder, 
                          chunk, search_criteria, progress_queue, result_queue, i)
                )
                thread.start()
                threads.append(thread)

            # 监控进度和收集结果
            completed_threads = 0
            thread_progress = {i: 0 for i in range(len(threads))}
            thread_totals = {i: len(message_chunks[i]) for i in range(len(message_chunks))}

            while completed_threads < len(threads):
                try:
                    # 检查进度队列
                    while not progress_queue.empty():
                        try:
                            msg_type, thread_id, current, total = progress_queue.get_nowait()
                            if msg_type == 'progress':
                                thread_progress[thread_id] = current
                                
                                # 计算总体进度
                                total_processed = sum(thread_progress.values())
                                overall_progress = int((total_processed / total_messages) * 100)
                                if callback:
                                    callback(overall_progress, f"正在处理邮件 {total_processed}/{total_messages} (使用{len(threads)}个线程)")
                        except queue.Empty:
                            break

                    # 检查结果队列
                    while not result_queue.empty():
                        try:
                            msg_type, thread_id, data = result_queue.get_nowait()
                            if msg_type == 'success':
                                mail_records.extend(data)
                                logger.info(f"Thread {thread_id}: 成功处理 {len(data)} 封邮件")
                                completed_threads += 1
                            elif msg_type == 'error':
                                logger.error(f"Thread {thread_id}: 处理失败 - {data}")
                                completed_threads += 1
                        except queue.Empty:
                            break

                    time.sleep(0.1)  # 避免过度占用CPU

                except Exception as e:
                    logger.error(f"监控线程进度失败: {str(e)}")
                    break

            # 等待所有线程完成
            for thread in threads:
                thread.join(timeout=30)  # 30秒超时

            # 记录完成日志
            log_email_complete(email_address, "未知", len(mail_records), len(mail_records), len(mail_records))
            logger.info(f"多线程处理完成，共获得 {len(mail_records)} 封邮件")

            return mail_records

        except Exception as e:
            logger.error(f"获取邮件失败: {str(e)}")
            log_email_error(email_address, "未知", str(e))
            if mail:
                try:
                    mail.close()
                    mail.logout()
                except:
                    pass
            return []

    @staticmethod
    @timing_decorator
    def check_mail(email_info, db, progress_callback=None):
        """检查邮箱中的新邮件"""
        try:
            email_address = email_info['email']
            password = email_info['password']
            server = email_info.get('server', 'imap.gmail.com')
            port = email_info.get('port', 993)
            use_ssl = email_info.get('use_ssl', True)

            # 创建进度回调
            def folder_progress_callback(progress, folder):
                if progress_callback:
                    progress_callback(progress, f"正在检查文件夹: {folder}")

            # 获取邮件
            mail_records = IMAPMailHandler.fetch_emails(
                email_address=email_address,
                password=password,
                server=server,
                port=port,
                use_ssl=use_ssl,
                callback=folder_progress_callback
            )

            if not mail_records:
                if progress_callback:
                    progress_callback(0, "没有找到新邮件")
                return {'success': False, 'message': '没有找到新邮件'}

            # 循环打印 has_attachments 和 full_attachments
            # for idx, record in enumerate(mail_records):
            #     has_attachments = record.get("has_attachments", False)
            #     full_attachments = record.get("full_attachments", [])
            #     logger.info(f"[MailRecord {idx}] subject: {record.get('subject')[:30]}, has_attachments: {has_attachments}, full_attachments: {len(full_attachments)}")
            
            # 导入 MailProcessor 以使用其 save_mail_records 方法
            from .mail_processor import MailProcessor
            
            # 保存邮件记录 - 使用 MailProcessor 的方法以支持附件处理
            saved_count = MailProcessor.save_mail_records(db, email_info['id'], mail_records, progress_callback)

            if progress_callback:
                progress_callback(100, f"成功获取 {len(mail_records)} 封邮件，新增 {saved_count} 封")

            return {
                'success': True,
                'message': f'成功获取 {len(mail_records)} 封邮件，新增 {saved_count} 封'
            }

        except Exception as e:
            logger.error(f"检查邮件失败: {str(e)}")
            if progress_callback:
                progress_callback(0, f"检查邮件失败: {str(e)}")
            return {'success': False, 'message': str(e)}

