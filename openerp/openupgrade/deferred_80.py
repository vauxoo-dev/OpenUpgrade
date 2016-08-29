# -*- coding: utf-8 -*-
##############################################################################
#
#    OpenERP, Open Source Management Solution
#    This module copyright (C) 2014 Therp BV (<http://therp.nl>)
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
"""
This module can be used to contain functions that should be called at the end
of the migration. A migration may be run several times after corrections in
the code or the configuration, and there is no way for OpenERP to detect a
succesful result. Therefore, the functions in this module should be robust
against being run multiple times on the same database.
"""
import logging
from openerp import SUPERUSER_ID
from openerp.openupgrade import openupgrade
logger = logging.getLogger('OpenUpgrade.deferred')


def migrate_product_valuation(cr, pool):
    """Migrate the product valuation to a property field on product template.
    This field was moved to a new module which is not installed when the
    migration starts and thus not upgraded, which is why we do it here in the
    deferred step.

    This method removes the preserved legacy column upon success, to prevent
    double runs which would be harmful.
    """
    valuation_column = openupgrade.get_legacy_name('valuation')
    if not openupgrade.column_exists(cr, 'product_product', valuation_column):
        return

    cr.execute(
        """
        SELECT id FROM ir_model_fields
        WHERE model = 'product.template' AND name = 'valuation'
        """)
    field_id = cr.fetchone()[0]

    default = 'manual_periodic'  # as per stock_account/stock_account_data.xml

    cr.execute(
        """
        SELECT product_tmpl_id, {column} FROM product_product
        WHERE {column} != %s""".format(column=valuation_column),
        (default,))
    products = cr.fetchall()
    logger.debug(
        "Migrating the valuation field of %s products with non-default values",
        len(products))

    seen_ids = []
    for template_id, value in products:
        if not value or template_id in seen_ids:
            continue
        seen_ids.append(template_id)
        cr.execute('''
                   INSERT INTO ir_property (create_uid, create_date, write_uid,
                   write_date, fields_id, res_id, name, type, value_text)
                   VALUES (%s, NOW(), %s, NOW(), %s, %s, 'valuation',
                           'selection', %s)
                   ''', (SUPERUSER_ID, SUPERUSER_ID, field_id,
                         'product.template,{}'.format(template_id), value))
    cr.execute(
        "ALTER TABLE product_product DROP COLUMN {}".format(valuation_column))


def migrate_procurement_order_method(cr, pool):
    """Procurements method: change the supply_method for the matching rule

    Needs to be deferred because the rules are created in the migration
    of stock, purchase and mrp.

    Will only run if stock is installed. Will run after every attempt to
    upgrade, but harmless when run multiple times.
    """

    cr.execute(
        """
        SELECT id FROM ir_module_module
        WHERE name = 'stock' AND state = 'installed'
        AND latest_version = '8.0.1.1'
        """)
    if not cr.fetchone():
        # Only warn if there are traces of stock
        if openupgrade.table_exists(cr, 'stock_move'):
            logger.debug(
                "Stock not installed or not properly migrated, skipping "
                "migration of procurement orders.")
        return

    procure_method_legacy = openupgrade.get_legacy_name('procure_method')
    if not openupgrade.column_exists(
            cr, 'product_template', procure_method_legacy):
        # in this case, there was no migration for the procurement module
        # which can be okay if procurement was not installed in the 7.0 db
        return

    sql = '''
DROP FUNCTION IF EXISTS assign_procurement_rule(procurement procurement_order);
CREATE OR REPLACE FUNCTION assign_procurement_rule(procurement procurement_order)
RETURNS integer AS $$
    DECLARE
        rule integer;
        location stock_location%rowtype;
        paction varchar;
        supply varchar;
    BEGIN
        supply := (SELECT pt.{sup_method}
                   FROM product_product AS p
                   INNER JOIN product_template AS pt ON pt.id = p.product_tmpl_id LIMIT 1);
        SELECT * INTO location FROM stock_location WHERE id=procurement.location_id;
        IF procurement.{pro_method} = 'make_to_order' THEN
            IF location.usage = 'internal' THEN
                IF supply = 'manufacture' THEN
                    paction := 'manufacture';
                ELSE
                    paction := 'buy';
                END IF;
                rule := (SELECT id FROM procurement_rule WHERE location_id=location.id AND action=paction LIMIT 1);
            ELSE
                rule := (SELECT id FROM procurement_rule WHERE location_id=location.id AND action='make_to_order' LIMIT 1);
            END IF;
        ELSE
            rule := (SELECT id FROM procurement_rule WHERE location_id=location.id AND action='make_to_stock' LIMIT 1);
        END IF;
        IF rule is not null THEN
            UPDATE procurement_order SET rule_id=rule WHERE id=procurement.id;
        END IF;

        RETURN rule; END;
    $$ LANGUAGE plpgsql;
    '''.format(pro_method=procure_method_legacy,
               sup_method=openupgrade.get_legacy_name('supply_method'))
    cr.execute(sql)

    logger.debug(
        "Trying to find rules for procurements")
    cr.execute('''SELECT assign_procurement_rule(procu)
                  FROM procurement_order AS procu
                  WHERE rule_id is null
                        AND state != 'done'
               ''')
    cr.execute(
        """
        SELECT p.id,
               p.%s,
               l.id,
               l.usage,
               p.product_id,
               l.name
        FROM procurement_order AS p
        INNER JOIN stock_location AS l ON l.id=p.location_id
        WHERE rule_id is NULL AND state != %%s
        """ % procure_method_legacy, ('done',))

    procurements = cr.fetchall()
    for procur in procurements:
        logger.warn(
            "Procurement order #%s with location %s "
            "has no %s procurement rule, please create and "
            "assign a new rule for this procurement""",
            procur[0], procur[5],
            procur[1])



def migrate_stock_move_warehouse(cr):
    """
    If a database featured multiple shops with the same company but a
    different warehouse, we can now propagate this warehouse to the
    associated stock moves. The warehouses were written on the procurements
    in the sale_stock module, while the moves were associated with the
    procurements in purchase and mrp. The order of processing between
    these modules seems to be independent, which is why we do this here
    in the deferred step.
    """
    cr.execute(
        "SELECT * FROM ir_module_module WHERE name='stock' "
        "AND state='installed'")
    if not cr.fetchone():  # No stock
        return
    openupgrade.logged_query(
        cr,
        """
        UPDATE stock_move sm
        SET warehouse_id = po.warehouse_id
        FROM procurement_order po
        WHERE sm.procurement_id = po.id
            OR po.move_dest_id = sm.id
        """)

def delete_wrong_views(cr):
    """
    Delete wrong dependencies views
    """
    cr.execute('''
DROP FUNCTION IF EXISTS delete_old_views(view ir_ui_view);
CREATE OR REPLACE FUNCTION delete_old_views(view ir_ui_view)
RETURNS varchar AS $$
    DECLARE
        deleted varchar;
        inherit_view ir_ui_view%rowtype;
    BEGIN
        deleted := view.name;
        FOR inherit_view IN SELECT * FROM ir_ui_view WHERE inherit_id=view.id LOOP
            PERFORM delete_old_views(inherit_view);
        END LOOP;
        DELETE FROM ir_ui_view WHERE id=view.id;
        RETURN deleted; END;
    $$ LANGUAGE plpgsql;
               ''')
    cr.execute(
        """
        SELECT delete_old_views(views) AS name
        FROM ir_ui_view AS views
        WHERE arch ilike '%%prodlot_id%%'
              AND model='stock.move';
        """)

    cr.execute(
        """
DELETE FROM ir_ui_view WHERE arch ilike '%%xpath%%button%%429%%';
        """)

    cr.execute(
        """
SELECT delete_old_views(views) AS name
FROM ir_ui_view AS views
WHERE arch ilike '%%required_date_product%%' AND model='purchase.order';
        """)

    cr.execute(
        """
SELECT delete_old_views(views) AS name
FROM ir_ui_view AS views
WHERE arch ilike '%%xpath%%string%%Expected Date%%' AND model='purchase.order';
        """)

    cr.execute(
        """
SELECT delete_old_views(views) AS name
FROM ir_ui_view AS views
WHERE arch ilike '%%procurement_id%%' AND model='stock.warehouse.orderpoint';
        """)

    cr.execute(
        """
SELECT delete_old_views(views) AS name
FROM ir_ui_view AS views
WHERE arch ilike '%%xpath%%button%action_process%%' AND model='stock.picking';
        """)

    cr.execute(
        """
SELECT delete_old_views(views) AS name
FROM ir_ui_view AS views
WHERE arch ilike '%%stock_journal_id%%' AND model='stock.picking';
        """)

    cr.execute(
        """
        SELECT delete_old_views(views) AS name
        FROM ir_ui_view AS views
        WHERE model in ('stock.picking.in', 'stock.picking.out',
                        'stock.picking.int');
        """)


def migrate_deferred(cr, pool):
    migrate_product_valuation(cr, pool)
    # migrate_procurement_order_method(cr, pool)
    migrate_stock_move_warehouse(cr)
    delete_wrong_views(cr)
