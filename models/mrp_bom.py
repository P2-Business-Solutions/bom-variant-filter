from odoo import models, api
import logging

_logger = logging.getLogger(__name__)


class MrpBom(models.Model):
    _inherit = 'mrp.bom'

    @api.model
    def _bom_find(self, products, picking_type=None, company_id=False, bom_type=False):
        """
        Override _bom_find to skip BOM candidates where no component lines
        apply to the requested product variant.

        Standard Odoo selects the highest-priority (lowest sequence) BOM that
        matches the product template/variant, without checking whether any of
        its component lines actually resolve for the variant. This means a
        precut BOM (sequence=10) would be selected for a PC variant even though
        all its lines are restricted to Glass — resulting in an empty MO.

        This override:
          1. Lets super() pick the normal winner.
          2. If that BOM has zero applicable lines for the variant, searches for
             the next candidate (by sequence) that does have applicable lines.
          3. Returns an empty BOM recordset if no candidate qualifies, rather
             than returning a BOM that would produce a blank manufacturing order.
        """
        result = super()._bom_find(
            products,
            picking_type=picking_type,
            company_id=company_id,
            bom_type=bom_type,
        )

        for product, bom in list(result.items()):
            if not bom:
                continue

            if self._bom_has_applicable_lines(bom, product):
                # Normal case — primary BOM is fine, nothing to do.
                continue

            _logger.debug(
                "BOM '%s' (id=%s) has no applicable lines for variant '%s'. "
                "Searching for fallback BOM.",
                bom.display_name, bom.id, product.display_name,
            )

            fallback = self._find_fallback_bom(
                product, bom, picking_type, company_id, bom_type
            )

            if fallback:
                _logger.debug(
                    "Fallback BOM '%s' (id=%s) selected for variant '%s'.",
                    fallback.display_name, fallback.id, product.display_name,
                )
            else:
                _logger.warning(
                    "No applicable BOM found for variant '%s' — returning empty.",
                    product.display_name,
                )

            result[product] = fallback

        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _bom_has_applicable_lines(self, bom, product):
        """
        Return True if at least one component line on `bom` is not skipped
        for `product`.

        A BOM with no lines at all returns False so it won't be selected as a
        silent no-op over a better candidate.
        """
        if not bom.bom_line_ids:
            return False
        return any(
            not line._skip_bom_line(product)
            for line in bom.bom_line_ids
        )

    def _find_fallback_bom(self, product, exclude_bom, picking_type, company_id, bom_type):
        """
        Search for the next-best BOM for `product`, excluding `exclude_bom`,
        returning the first one (by sequence, then id) that has at least one
        applicable component line.
        """
        domain = [
            ('active', '=', True),
            ('id', '!=', exclude_bom.id),
            # Match on template, and allow either a specific-variant BOM for
            # this exact variant or an unspecified (template-level) BOM.
            ('product_tmpl_id', '=', product.product_tmpl_id.id),
            '|',
            ('product_id', '=', product.id),
            ('product_id', '=', False),
        ]

        if bom_type:
            domain += [('type', '=', bom_type)]

        if picking_type:
            domain += [
                '|',
                ('picking_type_id', '=', picking_type.id),
                ('picking_type_id', '=', False),
            ]

        if company_id:
            domain += [
                '|',
                ('company_id', '=', company_id),
                ('company_id', '=', False),
            ]

        candidates = self.search(domain, order='sequence, id')

        for candidate in candidates:
            if self._bom_has_applicable_lines(candidate, product):
                return candidate

        return self.env['mrp.bom']
