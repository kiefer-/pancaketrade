from datetime import datetime
from decimal import Decimal
from typing import Mapping, NamedTuple

from pancaketrade.network import Network
from pancaketrade.persistence import Order, db
from pancaketrade.utils.config import Config
from pancaketrade.utils.generic import chat_message, check_chat_id
from pancaketrade.watchers import OrderWatcher, TokenWatcher
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    Filters,
    MessageHandler,
)


class BuySellResponses(NamedTuple):
    TYPE: int = 0
    TRAILING: int = 1
    AMOUNT: int = 3
    SUMMARY: int = 6


class BuySellConversation:
    def __init__(self, parent, config: Config):
        self.parent = parent
        self.net: Network = parent.net
        self.config = config
        self.next = BuySellResponses()
        self.handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.command_buysell, pattern='^buy_sell:0x[a-fA-F0-9]{40}$')],
            states={
                self.next.TYPE: [CallbackQueryHandler(self.command_buysell_type, pattern='^[^:]*$')],
                self.next.TRAILING: [
                    CallbackQueryHandler(self.command_buysell_trailing, pattern='^[^:]*$'),
                    MessageHandler(Filters.text & ~Filters.command, self.command_buysell_trailing),
                ],
                self.next.AMOUNT: [
                    CallbackQueryHandler(self.command_buysell_amount, pattern='^[^:]*$'),
                    MessageHandler(Filters.text & ~Filters.command, self.command_buysell_amount),
                ],
                self.next.SUMMARY: [
                    CallbackQueryHandler(self.command_buysell_summary, pattern='^[^:]*$'),
                ],
            },
            fallbacks=[CommandHandler('cancelbuysell', self.command_cancelbuysell)],
            name='buysell_conversation',
            conversation_timeout=300,
        )

    @check_chat_id
    def command_buysell(self, update: Update, context: CallbackContext):
        assert update.callback_query and context.user_data is not None
        query = update.callback_query
        # query.answer()
        assert query.data
        token_address = query.data.split(':')[1]
        token = self.parent.watchers[token_address]
        context.user_data['buysell'] = {'token_address': token_address}
        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton('🟢 Buy', callback_data='buy'),
                    InlineKeyboardButton('🔴 Sell', callback_data='sell'),
                ],
                [
                    InlineKeyboardButton('❌ Cancel', callback_data='cancel'),
                ],
            ]
        )
        chat_message(
            update,
            context,
            text=f'Which <u>type of transaction</u> would you like to create for {token.name}?',
            reply_markup=reply_markup,
            edit=False,
        )
        return self.next.TYPE

    @check_chat_id
    def command_buysell_type(self, update: Update, context: CallbackContext):
        assert update.callback_query and context.user_data is not None
        query = update.callback_query
        # query.answer()
        if query.data == 'cancel':
            self.cancel_command(update, context)
            return ConversationHandler.END
        order = context.user_data['buysell']
        token = self.parent.watchers[order['token_address']]
        if query.data not in ['buy', 'sell']:
            self.command_error(update, context, text='That type of transaction is not supported.')
            return ConversationHandler.END
        order['type'] = query.data
        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton('1%', callback_data='1'),
                    InlineKeyboardButton('2%', callback_data='2'),
                    InlineKeyboardButton('5%', callback_data='5'),
                    InlineKeyboardButton('10%', callback_data='10'),
                ],
                [
                    InlineKeyboardButton('No trailing stop loss', callback_data='None'),
                    InlineKeyboardButton('❌ Cancel', callback_data='cancel'),
                ],
            ]
        )
        chat_message(
            update,
            context,
            text=f'OK, the order will {order["type"]} {token.name}.'
            + 'Do you want to enable <u>trailing stop loss</u>? If yes, what is the callback rate?\n'
            + 'You can also message me a custom value in percent.',
            reply_markup=reply_markup,
        )
        return self.next.TRAILING

    @check_chat_id
    def command_buysell_trailing(self, update: Update, context: CallbackContext):
        assert update.effective_chat and context.user_data is not None
        order = context.user_data['buysell']
        token = self.parent.watchers[order['token_address']]
        unit = 'BNB' if order['type'] == 'buy' else token.symbol
        balance = (
            self.net.get_bnb_balance()
            if order['type'] == 'buy'
            else self.net.get_token_balance(token_address=token.address)
        )
        reply_markup = (
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton('25%', callback_data='0.25'),
                        InlineKeyboardButton('50%', callback_data='0.5'),
                        InlineKeyboardButton('75%', callback_data='0.75'),
                        InlineKeyboardButton('100%', callback_data='1.0'),
                    ],
                    [
                        InlineKeyboardButton('❌ Cancel', callback_data='cancel'),
                    ],
                ]
            )
            if order['type'] == 'sell'
            else InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel', callback_data='cancel')]])
        )
        if update.message is None:
            assert update.callback_query
            query = update.callback_query
            # query.answer()
            assert query.data
            if query.data == 'cancel':
                self.cancel_command(update, context)
                return ConversationHandler.END
            if query.data == 'None':
                order['trailing_stop'] = None
                chat_message(
                    update,
                    context,
                    text='OK, the order will use no trailing stop loss.\n'
                    + f'Next, <u>how much {unit}</u> do you want me to use for {order["type"]}ing?\n'
                    + f'You can use scientific notation like <code>{balance:.1E}</code> if you want.\n'
                    + f'Current balance: <b>{balance:.6g} {unit}</b>',
                    reply_markup=reply_markup,
                )
                return self.next.AMOUNT
            try:
                callback_rate = int(query.data)
            except ValueError:
                self.command_error(update, context, text='The callback rate is not recognized.')
                return ConversationHandler.END
        else:
            assert update.message and update.message.text
            try:
                callback_rate = int(update.message.text.strip())
            except ValueError:
                chat_message(update, context, text='⚠️ The callback rate is not recognized, try again:')
                return self.next.TRAILING
        order['trailing_stop'] = callback_rate
        chat_message(
            update,
            context,
            text=f'OK, the order will use trailing stop loss with {callback_rate}% callback.\n'
            + f'Next, <u>how much {unit}</u> do you want me to use for {order["type"]}ing?\n'
            + f'You can use scientific notation like <code>{balance:.1E}</code> if you want.\n'
            + f'Current balance: <b>{balance:.6g} {unit}</b>',
            reply_markup=reply_markup,
        )
        return self.next.AMOUNT

    @check_chat_id
    def command_buysell_amount(self, update: Update, context: CallbackContext):
        assert context.user_data is not None
        order = context.user_data['buysell']
        token = self.parent.watchers[order['token_address']]
        if update.message is None:  # we got a button callback, either cancel or fraction of balance
            assert update.callback_query
            query = update.callback_query
            # query.answer()
            if query.data == 'cancel':
                self.cancel_command(update, context)
                return ConversationHandler.END
            assert query.data is not None
            try:
                balance_fraction = Decimal(query.data)
            except Exception:
                self.command_error(update, context, text='The callback rate is not recognized.')
                return ConversationHandler.END
            balance = self.net.get_token_balance(token_address=token.address)
            amount = balance_fraction * balance
        else:
            assert update.message and update.message.text
            try:
                amount = Decimal(update.message.text.strip())
            except Exception:
                chat_message(update, context, text='⚠️ The amount you inserted is not valid. Try again:')
                return self.next.AMOUNT
        decimals = 18 if order['type'] == 'buy' else token.decimals
        bnb_price = self.net.get_bnb_price()
        limit_price = Decimal(order["limit_price"])
        usd_amount = bnb_price * amount if order['type'] == 'buy' else bnb_price * limit_price * amount
        unit = f'BNB worth of {token.symbol}' if order['type'] == 'buy' else token.symbol
        order['amount'] = str(int(amount * Decimal(10 ** decimals)))
        chat_message(
            update,
            context,
            text=f'OK, I will {order["type"]} {amount:.6g} {unit} (~${usd_amount:.2f}).\n'
            + '<u>Confirm</u> the order below!',
        )
        return self.print_summary(update, context)

    def print_summary(self, update: Update, context: CallbackContext):
        assert context.user_data is not None
        order = context.user_data['buysell']
        token: TokenWatcher = self.parent.watchers[order['token_address']]
        amount = self.get_human_amount(order, token)
        unit = self.get_amount_unit(order, token)
        trailing = (
            f'Trailing stop loss {order["trailing_stop"]}% callback\n' if order["trailing_stop"] is not None else ''
        )
        current_price, _ = self.net.get_token_price(
            token_address=token.address, token_decimals=token.decimals, sell=order['type'] == 'sell'
        )
        bnb_price = self.net.get_bnb_price()
        usd_amount = bnb_price * amount if order['type'] == 'buy' else bnb_price * current_price * amount
        message = (
            '<u>Preview:</u>\n' + f'{token.name}\n' + trailing + f'Amount: {amount:.6g} {unit} (${usd_amount:.2f})'
        )
        chat_message(
            update,
            context,
            text=message,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton('✅ Validate', callback_data='ok'),
                        InlineKeyboardButton('❌ Cancel', callback_data='cancel'),
                    ]
                ]
            ),
        )
        return self.next.SUMMARY

    @check_chat_id
    def command_createorder_summary(self, update: Update, context: CallbackContext):
        assert update.effective_chat and update.callback_query and context.user_data is not None
        query = update.callback_query
        # query.answer()
        if query.data != 'ok':
            self.cancel_command(update, context)
            return ConversationHandler.END
        add = context.user_data['buysell']
        token: TokenWatcher = self.parent.watchers[add['token_address']]
        del add['token_address']  # not needed in order record creation
        try:
            db.connect()
            with db.atomic():
                order_record = Order.create(token=token.token_record, created=datetime.now(), **add)
        except Exception:
            self.command_error(update, context, text='Failed to create database record.')
            return ConversationHandler.END
        finally:
            del context.user_data['buysell']
            db.close()
        order = OrderWatcher(
            order_record=order_record, net=self.net, dispatcher=context.dispatcher, chat_id=update.effective_chat.id
        )
        token.orders.append(order)
        chat_message(update, context, text='✅ Order was added successfully!')
        return ConversationHandler.END

    @check_chat_id
    def command_cancelbuysell(self, update: Update, context: CallbackContext):
        self.cancel_command(update, context)
        return ConversationHandler.END

    def get_human_amount(self, order: Mapping, token) -> Decimal:
        decimals = token.decimals if order['type'] == 'sell' else 18
        return Decimal(order['amount']) / Decimal(10 ** decimals)

    def get_amount_unit(self, order: Mapping, token) -> str:
        return token.symbol if order['type'] == 'sell' else 'BNB'

    def cancel_command(self, update: Update, context: CallbackContext):
        assert context.user_data is not None
        del context.user_data['buysell']
        chat_message(update, context, text='⚠️ OK, I\'m cancelling this command.', edit=False)

    def command_error(self, update: Update, context: CallbackContext, text: str):
        assert context.user_data is not None
        del context.user_data['buysell']
        chat_message(update, context, text=f'⛔️ {text}', edit=False)