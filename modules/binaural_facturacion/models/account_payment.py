from odoo import api, fields, models, _
from odoo.exceptions import RedirectWarning, UserError, ValidationError, AccessError
from odoo.tools import float_compare, date_utils, email_split, email_re
from odoo.tools.misc import formatLang, format_date, get_lang

from datetime import date, timedelta
from collections import defaultdict
from itertools import zip_longest
from hashlib import sha256
from json import dumps

import ast
import json
import re
import warnings

import logging
_logger = logging.getLogger(__name__)


class AccountPaymentBinauralFacturacion(models.Model):
    _inherit = 'account.payment'

    def default_alternate_currency(self):
        alternate_currency = int(self.env['ir.config_parameter'].sudo().get_param('curreny_foreign_id'))

        if alternate_currency:
            return alternate_currency
        else:
            return False

    def default_currency_rate(self):
        rate = 0
        alternate_currency = int(self.env['ir.config_parameter'].sudo().get_param('curreny_foreign_id'))
        if alternate_currency:
            currency = self.env['res.currency.rate'].search([('currency_id', '=', alternate_currency)], limit=1,
                                                            order='name desc')
            rate = currency.rate

        return rate

    foreign_currency_id = fields.Many2one('res.currency', default=default_alternate_currency, tracking=True)
    foreign_currency_rate = fields.Float(string="Tasa", tracking=True, default=default_currency_rate,
                                         currency_field='foreign_currency_id')
    foreign_currency_date = fields.Date(string="Fecha", default=fields.Date.today(), tracking=True)

    @api.onchange('foreign_currency_id', 'foreign_currency_rate')
    def _onchange_foreign_currency_rate(self):
        move_id = self.env['account.move'].browse(self.move_id.ids)
        move_id.update({'foreign_currency_rate': self.foreign_currency_rate})

    def _get_rate(self, foreign_currency_id, foreign_currency_date, operator):
        rate = self.env['res.currency.rate'].search([('currency_id', '=', foreign_currency_id),
                                                     ('name', operator, foreign_currency_date)], limit=1,
                                                    order='name desc')
        return rate

    @api.model_create_multi
    def create(self, vals_list):
        # OVERRIDE
        flag = False
        rate = 0
        for record in vals_list:
            rate = record.get('foreign_currency_rate', False)
            if rate:
                rate = round(rate, 2)
                alternate_currency = int(self.env['ir.config_parameter'].sudo().get_param('curreny_foreign_id'))
                if alternate_currency:
                    currency = self.env['res.currency.rate'].search([('currency_id', '=', alternate_currency)], limit=1,
                                                                    order='name desc')
                    if rate != currency.rate:
                        flag = True
        res = super(AccountPaymentBinauralFacturacion, self).create(vals_list)
        if flag:
            old_rate = self.default_currency_rate()
            # El usuario xxxx ha usado una tasa personalizada, la tasa del sistema para la fecha del pago xxx es de xxxx y ha usada la tasa personalizada xxx
            display_msg = "El usuario " + self.env.user.name + " ha usado una tasa personalizada,"
            display_msg += " la tasa del sistema para la fecha del pago " + str(fields.Date.today()) + " es de "
            display_msg += str(old_rate) + " y ha usada la tasa personalizada " + str(rate)
            res.message_post(body=display_msg)
        return res

    # def _prepare_move_line_default_vals(self, write_off_line_vals=None):
    #     """enviar tasa al crear el pago"""
    #     res = super(AccountPaymentBinauralFacturacion, self)._prepare_move_line_default_vals(write_off_line_vals)
    #     for record in res:
    #         record.setdefault('foreign_currency_rate', self.foreign_currency_rate)
    #     return res

    def _prepare_move_line_default_vals(self, write_off_line_vals=None):
        # TODO: se hace sobrecarga de la funcion _convert() -> modulo binaural_contactos_configuraciones
        ''' Prepare the dictionary to create the default account.move.lines for the current payment.
        :param write_off_line_vals: Optional dictionary to create a write-off account.move.line easily containing:
            * amount:       The amount to be added to the counterpart amount.
            * name:         The label to set on the line.
            * account_id:   The account on which create the write-off.
        :return: A list of python dictionary to be passed to the account.move.line's 'create' method.
        '''
        self.ensure_one()
        write_off_line_vals = write_off_line_vals or {}

        if not self.journal_id.payment_debit_account_id or not self.journal_id.payment_credit_account_id:
            raise UserError(_(
                "You can't create a new payment without an outstanding payments/receipts account set on the %s journal.",
                self.journal_id.display_name))

        # Compute amounts.
        write_off_amount = write_off_line_vals.get('amount', 0.0)

        if self.payment_type == 'inbound':
            # Receive money.
            counterpart_amount = -self.amount
            write_off_amount *= -1
        elif self.payment_type == 'outbound':
            # Send money.
            counterpart_amount = self.amount
        else:
            counterpart_amount = 0.0
            write_off_amount = 0.0

        balance = self.currency_id._convert(counterpart_amount, self.company_id.currency_id, self.company_id,
                                            self.date, True, self.foreign_currency_rate)
        counterpart_amount_currency = counterpart_amount
        write_off_balance = self.currency_id._convert(write_off_amount, self.company_id.currency_id, self.company_id,
                                                      self.date, True, self.foreign_currency_rate)
        write_off_amount_currency = write_off_amount
        currency_id = self.currency_id.id

        if self.is_internal_transfer:
            if self.payment_type == 'inbound':
                liquidity_line_name = _('Transfer to %s', self.journal_id.name)
            else: # payment.payment_type == 'outbound':
                liquidity_line_name = _('Transfer from %s', self.journal_id.name)
        else:
            liquidity_line_name = self.payment_reference

        # Compute a default label to set on the journal items.

        payment_display_name = {
            'outbound-customer': _("Customer Reimbursement"),
            'inbound-customer': _("Customer Payment"),
            'outbound-supplier': _("Vendor Payment"),
            'inbound-supplier': _("Vendor Reimbursement"),
        }

        default_line_name = self.env['account.move.line']._get_default_line_name(
            _("Internal Transfer") if self.is_internal_transfer else payment_display_name['%s-%s' % (self.payment_type, self.partner_type)],
            self.amount,
            self.currency_id,
            self.date,
            partner=self.partner_id,
        )

        line_vals_list = [
            # Liquidity line.
            {
                'name': liquidity_line_name or default_line_name,
                'date_maturity': self.date,
                'amount_currency': -counterpart_amount_currency,
                'currency_id': currency_id,
                'debit': balance < 0.0 and -balance or 0.0,
                'credit': balance > 0.0 and balance or 0.0,
                'partner_id': self.partner_id.id,
                'account_id': self.journal_id.payment_debit_account_id.id if balance < 0.0 else self.journal_id.payment_credit_account_id.id,
            },
            # Receivable / Payable.
            {
                'name': self.payment_reference or default_line_name,
                'date_maturity': self.date,
                'amount_currency': counterpart_amount_currency + write_off_amount_currency if currency_id else 0.0,
                'currency_id': currency_id,
                'debit': balance + write_off_balance > 0.0 and balance + write_off_balance or 0.0,
                'credit': balance + write_off_balance < 0.0 and -balance - write_off_balance or 0.0,
                'partner_id': self.partner_id.id,
                'account_id': self.destination_account_id.id,
            },
        ]
        if write_off_balance:
            # Write-off line.
            line_vals_list.append({
                'name': write_off_line_vals.get('name') or default_line_name,
                'amount_currency': -write_off_amount_currency,
                'currency_id': currency_id,
                'debit': write_off_balance < 0.0 and -write_off_balance or 0.0,
                'credit': write_off_balance > 0.0 and write_off_balance or 0.0,
                'partner_id': self.partner_id.id,
                'account_id': write_off_line_vals.get('account_id'),
            })
        return line_vals_list