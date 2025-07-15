import discord
import logging
import asyncio
import os
import certifi
import psutil
import time
import traceback
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from pymongo import MongoClient, errors
from pymongo.server_api import ServerApi
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager

load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')

if not DISCORD_TOKEN:
    raise ValueError("Discord 토큰이 설정되지 않았습니다.")
if not MONGO_URI:
    raise ValueError("MongoDB 연결 문자열이 설정되지 않았습니다.")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

class DatabaseConnection:
    def __init__(self):
        self.client = None
        self.db = None
        self.connect()

    def connect(self):
        try:
            self.client = MongoClient(
                MONGO_URI,
                server_api=ServerApi('1'),
                tlsCAFile=certifi.where(),
                connectTimeoutMS=5000,
                retryWrites=True,
                w='majority'
            )
            self.db = self.client['tumblbug_bot']
            self.client.admin.command('ping')
            logging.info("MongoDB 연결 성공")
        except Exception as e:
            logging.error(f"MongoDB 연결 실패: {e}")
            raise

    def ensure_connection(self):
        max_retries = 3
        retry_count = 0
        while retry_count < max_retries:
            try:
                self.client.admin.command('ping')
                return
            except (errors.ConnectionFailure, errors.ServerSelectionTimeoutError):
                retry_count += 1
                logging.warning(f"MongoDB 연결 재시도 {retry_count}/{max_retries}")
                if retry_count < max_retries:
                    time.sleep(1)
                    self.connect()
                else:
                    raise

class TumblbugBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True  # 서버 관련 이벤트를 받기 위해 guilds 인텐트 활성화
        super().__init__(command_prefix='!', intents=intents)
        self.db_connection = DatabaseConnection()
        self.config = self.db_connection.db['server_config']
        self.ensure_indexes()
        self.driver = None
        self.driver_lock = asyncio.Lock()
        self.memory_check_task = None

    async def on_guild_join(self, guild):
        """
        봇이 서버에 추가될 때 호출되는 이벤트입니다.
        """
        guild_id = guild.id
        logging.info(f"새로운 서버에 추가됨: {guild.name} (ID: {guild_id})")

        # 초기 쿼리 생성
        try:
            existing_config = self.config.find_one({'guild_id': guild_id})
            if not existing_config:
                new_config = {
                    'guild_id': guild_id,
                    'monitored_urls': {},
                    'notification_channels': []
                }
                self.config.insert_one(new_config)
                logging.info(f"서버 초기 쿼리 생성: {guild_id}")
        except Exception as e:
            logging.error(f"서버 초기 쿼리 생성 중 오류 발생: {e}")

    def ensure_indexes(self):
        try:
            self.config.drop_index('channel_id_1')
        except:
            pass
        self.config.create_index([("guild_id", 1)], unique=True)

    async def setup_hook(self):
        await self.tree.sync()
        self.check_prices.start()
        self.memory_check_task = asyncio.create_task(self.check_memory_and_restart())

    async def check_memory_and_restart(self):
        MEMORY_THRESHOLD = 500 * 1024 * 1024  # 500MB
        while True:
            memory_usage = check_memory_usage()
            if memory_usage > MEMORY_THRESHOLD:
                logging.warning(f"메모리 사용량 초과: {memory_usage} bytes")
                if self.driver:
                    self.driver.quit()
                    self.driver = None
            await asyncio.sleep(3600)  # 1시간마다 메모리 사용량 확인

    async def close(self):
        if hasattr(self, 'db_connection'):
            self.db_connection.client.close()
            logging.info("MongoDB 연결 종료")
        if self.driver:
            self.driver.quit()
            logging.info("Selenium WebDriver 종료")
        if self.memory_check_task:
            self.memory_check_task.cancel()
        await super().close()

    async def get_driver(self):
        async with self.driver_lock:
            if not self.driver:
                options = ChromeOptions()
                options.add_argument('--headless=new')
                options.add_argument('--disable-gpu')
                options.add_argument('--no-sandbox')
                options.add_argument('--disable-dev-shm-usage')
                options.add_argument('--disable-blink-features=AutomationControlled')
                options.add_argument('--window-size=1280,800')
                options.add_argument('--disable-infobars')
                options.add_argument('--no-single-process')
                options.add_argument('--log-level=3')
                options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Whale/4.32.315.22 Safari/537.36")

                prefs = {
                    "profile.managed_default_content_settings.images": 2,
                    "profile.managed_default_content_settings.stylesheets": 2,
                    "profile.managed_default_content_settings.plugins": 2,
                    "profile.managed_default_content_settings.javascript": 1
                }
                options.add_experimental_option("prefs", prefs)
                options.page_load_strategy = 'none'

                try:
                    service = ChromeService(
                        ChromeDriverManager().install(),
                        log_path="chromedriver.log"
                    )
                    self.driver = webdriver.Chrome(service=service, options=options)
                except WebDriverException as e:
                    logging.error(f"WebDriver 초기화 실패: {e}")
                    raise

            return self.driver

    async def get_project_data(self, url):
        try:
            driver = await self.get_driver()
            driver.get(url)

            wait = WebDriverWait(driver, 10)
            
            project_title = None
            current_funding = None
            image_url = None

            # 프로젝트 제목 가져오기 시도
            try:
                project_title_element = wait.until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, 'h1[class*="styled__ProjectTitle-sc-"]'))
                )
                project_title = project_title_element.text.strip()
            except TimeoutException as e:
                # 타임아웃 발생 시 스크린샷 저장
                screenshot_path = f"screenshot_timeout_{time.time()}.png"
                self.driver.save_screenshot(screenshot_path)
                logging.error(f"프로젝트 제목 요소를 찾을 수 없습니다 (Timeout): {url} - 로케이터: h1[class*=\"styled__ProjectTitle-sc-\"]")
                logging.error(f"스크린샷 저장됨: {screenshot_path}")
                logging.error(f"페이지 로딩 또는 요소 찾기 시간 초과: {url} - 오류: {e}")
                logging.error(f"현재 URL: {self.driver.current_url}")
                logging.error(f"페이지 소스 (일부): {self.driver.page_source[:500]}...") # 페이지 소스의 일부 출력
                raise # 예외 다시 발생시켜 문제 확인
            except WebDriverException as e:
                # WebDriver 관련 다른 오류 처리
                logging.error(f"WebDriver 오류 발생: {url} - 오류: {e}")
                logging.error(f"현재 URL: {self.driver.current_url}")
                raise
            except Exception as e:
                logging.error(f"예상치 못한 오류 발생: {url} - 오류: {e}", exc_info=True)
                raise

            # 펀딩 금액 가져오기 시도
            try:
                price_element = wait.until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, '[class*="FundingOverallStatus__StatusValue-"]'))
                )
                price_text = price_element.text.strip()
                
                current_funding = int(price_text.replace(',', '').replace('원', '').replace('%', '').replace('명', '').strip())
            except TimeoutException:
                logging.error(f"펀딩 금액 요소를 찾을 수 없습니다 (Timeout): {url} - 로케이터: [class*=\"FundingOverallStatus__StatusValue-\"]")
                raise # 외부 TimeoutException 핸들러로 다시 발생
            except NoSuchElementException:
                logging.error(f"펀딩 금액 요소를 찾을 수 없습니다 (NoSuchElement): {url} - 로케이터: [class*=\"FundingOverallStatus__StatusValue-\"]")
                raise # 외부 NoSuchElementException 핸들러로 다시 발생

            # 이미지 URL 가져오기 시도 (필수적이지 않다면 오류 발생 시 None 반환)
            try:
                image_element = wait.until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, '[class*="SingleCoverImage__ProjectCoverImage-"]'))
                )
                image_url = image_element.find_element(By.TAG_NAME, "img").get_attribute("src")
            except (TimeoutException, NoSuchElementException):
                logging.warning(f"프로젝트 이미지 요소를 찾을 수 없습니다: {url} (계속 진행)")
                image_url = None # 이미지는 필수가 아닐 경우 None으로 처리

            return project_title, current_funding, image_url

        except TimeoutException as e:
            logging.error(f"페이지 로딩 또는 요소 찾기 시간 초과: {url} - 오류: {e}")
            logging.error(f"현재 URL: {driver.current_url}")
            logging.error(f"트레이스백:\n{traceback.format_exc()}")
            return None, None, None
        except NoSuchElementException as e:
            logging.error(f"필수 요소를 찾을 수 없음: {url} - 오류: {e}")
            logging.error(f"현재 URL: {driver.current_url}")
            logging.error(f"트레이스백:\n{traceback.format_exc()}")
            return None, None, None
        except WebDriverException as e:
            logging.error(f"WebDriver 오류: {url} - 오류: {e}")
            logging.error(f"현재 URL: {driver.current_url}")
            logging.error(f"트레이스백:\n{traceback.format_exc()}")
            return None, None, None
        except Exception as e:
            logging.error(f"프로젝트 정보 가져오기 실패: {url} - 오류: {e}")
            logging.error(f"현재 URL: {driver.current_url}")
            logging.error(f"트레이스백:\n{traceback.format_exc()}")
            return None, None, None

    def format_price(self, price):
        return f"{price:,}원"

    async def update_server_config(self, guild_id, update_data):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.db_connection.ensure_connection()
                existing_config = self.config.find_one({'guild_id': guild_id})
                
                if existing_config:
                    result = self.config.update_one(
                        {'guild_id': guild_id},
                        update_data
                    )
                else:
                    new_config = {
                        'guild_id': guild_id,
                        'monitored_urls': {},
                        'notification_channels': []
                    }
                    if 'monitored_urls' in update_data.get('$set', {}):
                        new_config['monitored_urls'] = update_data['$set']['monitored_urls']
                    if 'notification_channels' in update_data.get('$set', {}):
                        new_config['notification_channels'] = update_data['$set']['notification_channels']
                    
                    result = self.config.insert_one(new_config)
                
                return result
            except Exception as e:
                if attempt == max_retries - 1:
                    logging.error(f"서버 설정 업데이트 실패: {e}")
                    raise
                await asyncio.sleep(1)

    async def send_notifications(self, guild_id, embed):
        self.db_connection.ensure_connection()
        server_config = self.config.find_one({'guild_id': guild_id})
        if not server_config or 'notification_channels' not in server_config:
            return

        for channel_id in server_config['notification_channels']:
            channel = self.get_channel(channel_id)
            if channel:
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException as e:
                    logging.error(f"메시지 전송 실패: {channel_id} - {e}")

    async def send_milestone_notification(self, guild_id, url, current_funding, milestone, project_title):
        embed = discord.Embed(
            title=f"🎉 {project_title} {self.format_price(milestone)} 달성!",
            description=f"✅ 모금액 : {self.format_price(current_funding)}",
            color=discord.Color.green(),
            url=url
        )
        await self.send_notifications(guild_id, embed)

    async def check_custom_thresholds(self, guild_id, project_id, url, current_funding, data, project_title):
        thresholds = data.get('thresholds', [])
        removed_thresholds = []

        for threshold in thresholds:
            if current_funding >= threshold:
                embed = discord.Embed(
                    title=f"🎯 {project_title} {self.format_price(threshold)} 달성!",
                    description=f"✅ 모금액 : {self.format_price(current_funding)}",
                    color=discord.Color.green(),
                    url=url
                )
                embed.add_field(name="프로젝트 링크", value=url, inline=False)
                await self.send_notifications(guild_id, embed)
                removed_thresholds.append(threshold)

        if removed_thresholds:
            data['thresholds'] = [t for t in thresholds if t not in removed_thresholds]
            await self.update_server_config(
                guild_id,
                {'$set': {f'monitored_urls.{project_id}.thresholds': data['thresholds']}}
            )

    @tasks.loop(minutes=1)
    async def check_prices(self):
        # MongoDB 연결을 확인합니다.
        self.db_connection.ensure_connection()
        # 모든 서버 설정을 가져옵니다.
        for server_config in self.config.find():
            guild_id = server_config['guild_id']
            monitored_urls = server_config.get('monitored_urls', {})

            # 각 모니터링 URL에 대해 반복합니다.
            for project_id, data in monitored_urls.items():
                url = data['url']  # 프로젝트 URL 가져오기
                try:
                    # 프로젝트 데이터를 가져옵니다.
                    _, current_funding, _ = await self.get_project_data(url)
                    if not current_funding:
                        continue

                    # 초기 펀딩 금액이 설정되지 않은 경우 설정합니다.
                    if not data.get('initial_funding'):
                        data['initial_funding'] = current_funding
                        await self.update_server_config(
                            guild_id,
                            {'$set': {f'monitored_urls.{project_id}.initial_funding': current_funding}}
                        )
                        # 모니터링 시작 메시지를 보냅니다.
                        embed = discord.Embed(
                            title="모니터링 시작",
                            description=f"초기 금액: {self.format_price(current_funding)}",
                            color=discord.Color.blue()
                        )
                        embed.add_field(name="프로젝트 링크", value=url, inline=False)
                        await self.send_notifications(guild_id, embed)
                        continue

                    # 현재 펀딩 금액이 초기 펀딩 금액보다 큰 경우
                    if current_funding > data['initial_funding']:
                        base = (data['initial_funding'] // 1000000) * 1000000
                        next_1m = base + 1000000
                        project_title = data.get('title', '알 수 없는 프로젝트')

                        # 다음 1백만 원 목표를 달성한 경우
                        if current_funding >= next_1m and next_1m > data['initial_funding']:
                            await self.send_milestone_notification(guild_id, url, current_funding, next_1m, project_title)
                            # MongoDB에 initial_funding 업데이트
                            await self.update_server_config(
                                guild_id,
                                {'$set': {f'monitored_urls.{project_id}.initial_funding': next_1m}}
                            )

                        # 사용자 정의 임계값을 확인합니다.
                        await self.check_custom_thresholds(guild_id, project_id, url, current_funding, data, project_title)

                except Exception as e:
                    logging.error(f"가격 체크 중 오류 발생: {url} - {e}")

def check_memory_usage():
    process = psutil.Process()
    return process.memory_info().rss

def main():
    bot = TumblbugBot()
    setup_commands(bot)
    
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        logging.error(f"봇 실행 중 오류 발생: {e}")
    finally:
        if hasattr(bot, 'db_connection'):
            bot.db_connection.client.close()
        if bot.driver:
            bot.driver.quit()

from urllib.parse import urlparse

def extract_project_id(url: str) -> str:
    parsed_url = urlparse(url)
    path_parts = parsed_url.path.strip('/').split('/')
    if path_parts:
        return path_parts[0]
    raise ValueError("프로젝트ID를 추출할 수 없습니다.")

def setup_commands(bot):
    project_group = app_commands.Group(name="프로젝트", description="텀블벅 프로젝트 관련 명령어")
    threshold_group = app_commands.Group(name="임계값", description="텀블벅 임계값 관련 명령어")
    test_group = app_commands.Group(name="테스트", description="알림 시스템 테스트 명령어")

    # "추가" 명령어
    @project_group.command(name="추가", description="텀블벅 프로젝트를 모니터링합니다")
    async def project_add(interaction: discord.Interaction, url: str):
        await interaction.response.defer()

        if not url.startswith('https://tumblbug.com/'):
            embed = discord.Embed(
                title="❌ 오류",
                description="올바른 텀블벅 URL을 입력해주세요.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        try:
            project_id = extract_project_id(url)
        except ValueError as e:
            embed = discord.Embed(
                title="❌ 오류",
                description=f"프로젝트ID를 추출할 수 없습니다: {e}",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        project_title, current_funding, image_url = await bot.get_project_data(url)
        if not project_title or not current_funding:
            embed = discord.Embed(
                title="❌ 오류",
                description="프로젝트 정보를 가져오는 데 실패했습니다.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        try:
            guild_id = interaction.guild_id
            server_config = bot.config.find_one({'guild_id': guild_id})

            if not server_config:
                server_config = {
                    'guild_id': guild_id,
                    'monitored_urls': {},
                    'notification_channels': []
                }
                bot.config.insert_one(server_config)

            await bot.update_server_config(
                guild_id,
                {
                    '$set': {
                        f'monitored_urls.{project_id}': {
                            'url': url,
                            'initial_funding': current_funding,
                            'thresholds': [],
                            'title': project_title
                        }
                    }
                }
            )

            embed = discord.Embed(
                title="✅ 프로젝트 추가",
                description=f"프로젝트 '{project_title}'의 모니터링을 시작했습니다.\n모금액 : {bot.format_price(current_funding)}",
                color=discord.Color.blue(),
                url=url
            )
            if image_url:
                embed.set_thumbnail(url=image_url)
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logging.error(f"프로젝트 추가 중 오류 발생: {e}")
            embed = discord.Embed(
                title="❌ 오류",
                description="프로젝트 추가 중 오류가 발생했습니다.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    # "중지" 명령어
    @project_group.command(name="중지", description="모니터링 중인 프로젝트를 중지합니다")
    async def project_stop(interaction: discord.Interaction, project_title: str):
        await interaction.response.defer()

        guild_id = interaction.guild_id
        server_config = bot.config.find_one({'guild_id': guild_id})
        
        if not server_config or not any(data['title'] == project_title for data in server_config.get('monitored_urls', {}).values()):
            embed = discord.Embed(
                title="❌ 오류",
                description="모니터링 중인 프로젝트가 아닙니다.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        try:
            # 프로젝트 제목에 해당하는 프로젝트 ID를 찾습니다.
            project_id = next(
                project_id for project_id, data in server_config['monitored_urls'].items()
                if data['title'] == project_title
            )
            
            # 서버 설정에서 프로젝트를 제거합니다.
            bot.config.update_one(
                {'guild_id': guild_id},
                {'$unset': {f'monitored_urls.{project_id}': ''}}
            )
            
            # 성공 메시지를 보냅니다.
            embed = discord.Embed(
                title="✅ 프로젝트 중지",
                description=f"프로젝트 '{project_title}'의 모니터링을 중지했습니다.",
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logging.error(f"프로젝트 중지 중 오류 발생: {e}")
            embed = discord.Embed(
                title="❌ 오류",
                description="프로젝트 중지 중 오류가 발생했습니다.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @project_stop.autocomplete('project_title')
    async def project_stop_autocomplete(interaction: discord.Interaction, current: str):
        guild_id = interaction.guild_id
        server_config = bot.config.find_one({'guild_id': guild_id})
        if server_config and 'monitored_urls' in server_config:
            # 프로젝트 목록을 title로 필터링하여 제공
            projects = [
                app_commands.Choice(name=data['title'], value=data['title'])
                for data in server_config['monitored_urls'].values()
                if current.lower() in data['title'].lower()
            ]
            return projects[:25]  # 최대 25개까지만 표시
        return []

    # "목록" 명령어
    @project_group.command(name="목록", description="모니터링 중인 프로젝트 목록을 확인합니다")
    async def project_list(interaction: discord.Interaction):
        await interaction.response.defer()

        guild_id = interaction.guild_id
        server_config = bot.config.find_one({'guild_id': guild_id})
        embeds = []

        # 모니터링 중인 프로젝트가 없는 경우 메시지를 보냅니다.
        if not server_config or not server_config.get('monitored_urls'):
            embed = discord.Embed(
                title="📋 모니터링 중인 프로젝트",
                description="모니터링 중인 프로젝트가 없습니다.",
                color=discord.Color.blue()
            )
            await interaction.followup.send(embed=embed)
            return

        # 각 프로젝트에 대한 정보를 가져와서 임베드를 생성합니다.
        for project_id, data in server_config['monitored_urls'].items():
            try:
                project_title, current_funding, image_url = await bot.get_project_data(data['url'])
                if not project_title:
                    project_title = data.get('title', '알 수 없는 프로젝트')

                project_embed = discord.Embed(
                    title=project_title,
                    description=f"모금액 : {bot.format_price(current_funding)}",
                    color=discord.Color.blue(),
                    url=data['url']
                )
                
                if image_url:
                    project_embed.set_thumbnail(url=image_url)

                # 임계값이 있는 경우 추가
                if data.get('thresholds'):
                    thresholds_text = '\n'.join(f"- {bot.format_price(t)}" for t in sorted(data['thresholds']))
                    project_embed.add_field(
                        name="설정된 알림",
                        value=thresholds_text,
                        inline=False
                    )

                embeds.append(project_embed)

            except Exception as e:
                logging.error(f"프로젝트 정보 확인 중 오류 발생: {data['url']} - {e}")
                error_embed = discord.Embed(
                    title="⚠️ 오류",
                    description=f"[프로젝트 링크]({data['url']}) 정보를 불러올 수 없습니다.",
                    color=discord.Color.red()
                )
                embeds.append(error_embed)

        # 임베드를 10개씩 나누어 전송합니다.
        for i in range(0, len(embeds), 10):
            await interaction.followup.send(embeds=embeds[i:i+10])

    # "채널설정" 명령어
    @bot.tree.command(name="채널설정", description="현재 채널을 알림 채널로 설정합니다")
    async def set_channel(interaction: discord.Interaction):
        await interaction.response.defer()

        if not interaction.guild:
            await interaction.followup.send("이 명령어는 서버에서만 사용 가능합니다.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        try:
            server_config = bot.config.find_one({'guild_id': guild_id})
            if not server_config:
                server_config = {
                    'guild_id': guild_id,
                    'monitored_urls': {},
                    'notification_channels': [interaction.channel_id]
                }
                bot.config.insert_one(server_config)
            else:
                notification_channels = server_config.get('notification_channels', [])
                if interaction.channel_id in notification_channels:
                    embed = discord.Embed(
                        title="❌ 오류",
                        description="이미 알림 채널로 설정되어 있습니다.",
                        color=discord.Color.red()
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return

                notification_channels.append(interaction.channel_id)
                bot.config.update_one(
                    {'guild_id': guild_id},
                    {'$set': {'notification_channels': notification_channels}}
                )

            embed = discord.Embed(
                title="✅ 알림 채널 추가",
                description="현재 채널이 알림 채널로 추가되었습니다.",
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed)
        
        except Exception as e:
            logging.error(f"채널 설정 중 오류 발생: {e}")
            embed = discord.Embed(
                title="❌ 오류",
                description="채널 설정 중 오류가 발생했습니다.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    # "채널해제" 명령어
    @bot.tree.command(name="채널해제", description="현재 채널을 알림에서 제외합니다")
    async def remove_channel(interaction: discord.Interaction):
        await interaction.response.defer()

        if not interaction.guild:
            await interaction.followup.send("이 명령어는 서버에서만 사용 가능합니다.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        try:
            server_config = bot.config.find_one({'guild_id': guild_id})
            if not server_config or 'notification_channels' not in server_config:
                embed = discord.Embed(
                    title="❌ 오류",
                    description="현재 서버에 설정된 알림 채널이 없습니다.",
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            notification_channels = server_config['notification_channels']
            if interaction.channel_id not in notification_channels:
                embed = discord.Embed(
                    title="❌ 오류",
                    description="이 채널은 알림 채널로 설정되어 있지 않습니다.",
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            notification_channels.remove(interaction.channel_id)
            bot.config.update_one(
                {'guild_id': guild_id},
                {'$set': {'notification_channels': notification_channels}}
            )
            
            embed = discord.Embed(
                title="✅ 알림 채널 해제",
                description="현재 채널이 알림 채널에서 제외되었습니다.",
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logging.error(f"채널 해제 중 오류 발생: {e}")
            embed = discord.Embed(
                title="❌ 오류",
                description="채널 해제 중 오류가 발생했습니다.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    # "임계값 추가" 명령어
    @threshold_group.command(name="추가", description="선택한 프로젝트에 임계값을 추가합니다")
    async def threshold_add(interaction: discord.Interaction, project_title: str, amount: int):
        await interaction.response.defer()

        guild_id = interaction.guild_id
        server_config = bot.config.find_one({'guild_id': guild_id})
        
        # 모니터링 중인 프로젝트가 아닌 경우 오류 메시지를 보냅니다.
        if not server_config or not any(data['title'] == project_title for data in server_config.get('monitored_urls', {}).values()):
            embed = discord.Embed(
                title="❌ 오류",
                description="모니터링 중인 프로젝트가 아닙니다.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        try:
            # 프로젝트 제목에 해당하는 프로젝트 ID를 찾습니다.
            project_id = next(
                project_id for project_id, data in server_config['monitored_urls'].items()
                if data['title'] == project_title
            )

            thresholds = server_config['monitored_urls'][project_id].get('thresholds', [])

            # 이미 설정된 임계값인 경우 오류 메시지를 보냅니다.
            if amount in thresholds:
                embed = discord.Embed(
                    title="❌ 오류",
                    description="이미 설정된 임계값입니다.",
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            thresholds.append(amount)
            thresholds.sort()
            
            # 서버 설정을 업데이트하여 임계값을 추가합니다.
            bot.config.update_one(
                {'guild_id': guild_id},
                {'$set': {f'monitored_urls.{project_id}.thresholds': thresholds}}
            )
            
            # 성공 메시지를 보냅니다.
            embed = discord.Embed(
                title="✅ 임계값 설정",
                description=f"{bot.format_price(amount)} 임계값이 설정되었습니다.",
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logging.error(f"임계값 추가 중 오류 발생: {e}")
            embed = discord.Embed(
                title="❌ 오류",
                description="임계값 추가 중 오류가 발생했습니다.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    # "임계값 제거" 명령어
    @threshold_group.command(name="제거", description="선택한 프로젝트의 모든 임계값을 제거합니다")
    async def threshold_remove(interaction: discord.Interaction, project_title: str):
        await interaction.response.defer()

        guild_id = interaction.guild_id
        server_config = bot.config.find_one({'guild_id': guild_id})
        
        # 모니터링 중인 프로젝트가 아닌 경우 오류 메시지를 보냅니다.
        if not server_config or not any(data['title'] == project_title for data in server_config.get('monitored_urls', {}).values()):
            embed = discord.Embed(
                title="❌ 오류",
                description="모니터링 중인 프로젝트가 아닙니다.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        try:
            # 프로젝트 제목에 해당하는 프로젝트 ID를 찾습니다.
            project_id = next(
                project_id for project_id, data in server_config['monitored_urls'].items()
                if data['title'] == project_title
            )

            # 서버 설정을 업데이트하여 모든 임계값을 제거합니다.
            bot.config.update_one(
                {'guild_id': guild_id},
                {'$set': {f'monitored_urls.{project_id}.thresholds': []}}
            )
            
            # 성공 메시지를 보냅니다.
            embed = discord.Embed(
                title="✅ 임계값 제거",
                description="모든 임계값이 제거되었습니다.",
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logging.error(f"임계값 제거 중 오류 발생: {e}")
            embed = discord.Embed(
                title="❌ 오류",
                description="임계값 제거 중 오류가 발생했습니다.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            
    # "임계값 목록" 명령어
    @threshold_group.command(name="목록", description="선택한 프로젝트의 임계값 목록을 확인합니다")
    async def threshold_list(interaction: discord.Interaction, project_title: str):
        await interaction.response.defer()

        guild_id = interaction.guild_id
        server_config = bot.config.find_one({'guild_id': guild_id})
        
        # 모니터링 중인 프로젝트가 아닌 경우 오류 메시지를 보냅니다.
        if not server_config or not any(data['title'] == project_title for data in server_config.get('monitored_urls', {}).values()):
            embed = discord.Embed(
                title="❌ 오류",
                description="모니터링 중인 프로젝트가 아닙니다.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        try:
            # 프로젝트 제목에 해당하는 프로젝트 ID를 찾습니다.
            project_id = next(
                project_id for project_id, data in server_config['monitored_urls'].items()
                if data['title'] == project_title
            )

            thresholds = server_config['monitored_urls'][project_id]['thresholds']
            
            # 설정된 임계값이 없는 경우 메시지를 보냅니다.
            if not thresholds:
                embed = discord.Embed(
                    title="📋 임계값 목록",
                    description="설정된 임계값이 없습니다.",
                    color=discord.Color.blue()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            # 임계값 목록을 임베드에 추가합니다.
            embed = discord.Embed(
                title="📋 설정된 임계값 목록",
                color=discord.Color.blue()
            )
            for threshold in sorted(thresholds):
                embed.add_field(name=f"{bot.format_price(threshold)}", value="", inline=False)
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logging.error(f"임계값 목록 조회 중 오류 발생: {e}")
            embed = discord.Embed(
                title="❌ 오류",
                description="임계값 목록 조회 중 오류가 발생했습니다.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @threshold_add.autocomplete('project_title')
    @threshold_remove.autocomplete('project_title')
    @threshold_list.autocomplete('project_title')
    async def threshold_autocomplete(interaction: discord.Interaction, current: str):
        guild_id = interaction.guild_id
        server_config = bot.config.find_one({'guild_id': guild_id})
        if server_config and 'monitored_urls' in server_config:
            projects = [
                app_commands.Choice(name=data['title'], value=data['title'])
                for data in server_config['monitored_urls'].values()
                if current.lower() in data['title'].lower()
            ]
            return projects[:25]
        return []

    @test_group.command(name="펀딩", description="특정 프로젝트의 펀딩 알림을 테스트합니다")
    @app_commands.describe(
        project_title="테스트할 프로젝트 제목",
        test_amount="테스트할 펀딩 금액 (숫자만 입력)"
    )
    async def test_funding(
        interaction: discord.Interaction,
        project_title: str,
        test_amount: int
    ):
        await interaction.response.defer(ephemeral=True)
        
        # 서버 정보 확인
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.followup.send("서버에서만 사용 가능한 명령어입니다.", ephemeral=True)
            return
        
        try:
            # 프로젝트 정보 조회
            server_config = bot.config.find_one({'guild_id': guild_id})
            if not server_config or project_title not in [data['title'] for data in server_config['monitored_urls'].values()]:
                embed = discord.Embed(
                    title="❌ 오류",
                    description="모니터링 중인 프로젝트가 아닙니다.",
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            
            # 프로젝트 데이터 추출
            project_id = next(
                pid for pid, data in server_config['monitored_urls'].items()
                if data['title'] == project_title
            )
            project_data = server_config['monitored_urls'][project_id]
            
            # 금액 유효성 검증
            if test_amount < project_data['initial_funding']:
                embed = discord.Embed(
                    title="❌ 유효하지 않은 금액",
                    description=f"테스트 금액은 초기 금액({bot.format_price(project_data['initial_funding'])})보다 커야 합니다.",
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            
            # 가상 데이터로 알림 트리거
            test_current_funding = test_amount
            await bot.check_custom_thresholds(
                guild_id,
                project_id,
                project_data['url'],
                test_current_funding,
                project_data,
                project_title
            )
            
            # 1백만원 단위 알림도 트리거
            base = (project_data['initial_funding'] // 1000000) * 1000000
            next_1m = base + 1000000
            if test_current_funding >= next_1m:
                await bot.send_milestone_notification(
                    guild_id,
                    project_data['url'],
                    test_current_funding,
                    next_1m,
                    project_title
                )
            
            # 성공 메시지
            embed = discord.Embed(
                title="✅ 테스트 완료",
                description=f"**[{project_title}]**\n가상 펀딩 금액: {bot.format_price(test_current_funding)}",
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logging.error(f"펀딩 테스트 실패: {e}")
            await interaction.followup.send("❌ 테스트 중 오류가 발생했습니다.", ephemeral=True)

    # 프로젝트 제목 자동완성
    @test_funding.autocomplete('project_title')
    async def test_funding_autocomplete(interaction: discord.Interaction, current: str):
        return await threshold_autocomplete(interaction, current)

    bot.tree.add_command(project_group)
    bot.tree.add_command(threshold_group)
    bot.tree.add_command(test_group)

if __name__ == '__main__':
    main()