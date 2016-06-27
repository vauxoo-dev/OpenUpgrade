# -*- coding: utf-8 -*-
##############################################################################
#
#    Copyright (C) 2014 ONESTEiN B.V.
#              (C) 2014 Therp BV
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

from openerp.openupgrade import openupgrade, openupgrade_80
from openerp import pooler, SUPERUSER_ID


def post_messages(cr, pool):
    """ The obsolete message and note fields on procurements are replaced
    by posting messages on the chatter. Posting existing messages here."""
    admin_user = pool['res.users'].browse(cr, SUPERUSER_ID, SUPERUSER_ID)
    admin_partner_id = admin_user.partner_id.id

    # bypass message_post because it's forbiddingly slow
    cr.execute(
        """
        INSERT INTO mail_message
        (create_uid, create_date, author_id, model, res_id, body, type)
        SELECT %s, now(), %s, 'procurement.order', id,
        '<p>'||replace(replace({note}, '<', '&lt;'), '&', '&quot;')||'</p>',
        'notification'
        FROM procurement_order WHERE {note} IS NOT NULL AND {note} <> ''
        UNION
        SELECT %s, now(), %s, 'procurement.order', id,
        '<p>'||replace(replace({message}, '<', '&lt;'), '&', '&quot;')||'</p>',
        'notification'
        FROM procurement_order WHERE {message} IS NOT NULL AND {message} <> ''
        """.format(
            note=openupgrade.get_legacy_name('note'),
            message=openupgrade.get_legacy_name('message'),
        ),
        (SUPERUSER_ID, admin_partner_id, SUPERUSER_ID, admin_partner_id)
    )


def process_states(cr):
    """Map obsolete active states to 'running' and let the scheduler decide
    if these procurements are actually 'done'. Warn if there are procurements
    in obsolete draft state"""
    openupgrade.logged_query(
        cr, "UPDATE procurement_order SET state = %s WHERE state in %s",
        ('running', ('ready', 'waiting')))
    cr.execute(
        "SELECT COUNT(*) FROM procurement_order WHERE state = 'draft'")
    count = cr.fetchone()[0]
    if count:
        openupgrade.message(
            cr, 'procurement', 'procurement_order', 'state',
            'In this database, %s procurements are in draft state. In '
            'Odoo 8.0, these procurements cannot be processed further.',
            count)


@openupgrade.migrate()
def migrate(cr, version):
    pool = pooler.get_pool(cr.dbname)
    post_messages(cr, pool)
    process_states(cr)
    openupgrade.load_data(
        cr, 'procurement', 'migrations/8.0.1.0/noupdate_changes.xml')
