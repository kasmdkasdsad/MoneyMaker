-- Demo dataset: one live neighbourhood in Orlando, two pickup points, one
-- supplier, and campaigns in each interesting state (secured, near-miss, fresh).
-- Apply after schema.sql. Idempotent via fixed UUIDs + ON CONFLICT.

BEGIN;

INSERT INTO users (id, phone_e164, email, display_name, home_lat, home_lon, is_captain) VALUES
  ('a0000000-0000-4000-8000-000000000001','+14075550101','maya@example.com','Maya (captain)',28.5570,-81.3400,TRUE),
  ('a0000000-0000-4000-8000-000000000002','+14075550102','devon@example.com','Devon (captain)',28.5640,-81.3350,TRUE),
  ('a0000000-0000-4000-8000-000000000003','+14075550103',NULL,'Rosa',28.5575,-81.3405,FALSE),
  ('a0000000-0000-4000-8000-000000000004','+14075550104',NULL,'Tomas',28.5568,-81.3398,FALSE),
  ('a0000000-0000-4000-8000-000000000005','+14075550105',NULL,'Aiko',28.5645,-81.3348,FALSE)
ON CONFLICT (id) DO NOTHING;

-- Cost parameters are the launch estimates from docs/04-go-to-market.md:
-- a $60 line haul into the zone and a $12 marginal cost per stop.
INSERT INTO neighborhoods (id, name, city, centroid_lat, centroid_lon, radius_m,
                           delivery_dow, line_haul_cost_cents, stop_cost_cents, is_live) VALUES
  ('b0000000-0000-4000-8000-000000000001','Audubon Park','Orlando',28.5600,-81.3380,2500,3,6000,1200,TRUE)
ON CONFLICT (id) DO NOTHING;

INSERT INTO pickup_points (id, neighborhood_id, host_user_id, label, address_line,
                           lat, lon, capacity_units, commission_bps) VALUES
  ('c0000000-0000-4000-8000-000000000001','b0000000-0000-4000-8000-000000000001',
   'a0000000-0000-4000-8000-000000000001','Maple Court lobby','1412 Maple Ct',28.5570,-81.3400,200,500),
  ('c0000000-0000-4000-8000-000000000002','b0000000-0000-4000-8000-000000000001',
   'a0000000-0000-4000-8000-000000000002','Corrine Dr porch','2210 Corrine Dr',28.5640,-81.3350,120,500)
ON CONFLICT (id) DO NOTHING;

INSERT INTO suppliers (id, name, contact_email, lead_time_days, min_order_units) VALUES
  ('d0000000-0000-4000-8000-000000000001','Bulk Pantry Co','sales@bulkpantry.example',3,24)
ON CONFLICT (id) DO NOTHING;

INSERT INTO products (id, supplier_id, title, description, category) VALUES
  ('e0000000-0000-4000-8000-000000000001','d0000000-0000-4000-8000-000000000001',
   'Cold-pressed olive oil, 1L','Single-estate, harvested this season.','pantry'),
  ('e0000000-0000-4000-8000-000000000002','d0000000-0000-4000-8000-000000000001',
   'Laundry detergent, 5L','Fragrance-free concentrate, ~100 washes.','household')
ON CONFLICT (id) DO NOTHING;

-- case_pack_units is the supplier's shipping unit. Campaign thresholds must
-- round to it, which is frequently the binding constraint on the minimum.
INSERT INTO product_variants (id, product_id, sku, unit_label, case_pack_units, weight_grams, zone) VALUES
  ('f0000000-0000-4000-8000-000000000001','e0000000-0000-4000-8000-000000000001','OIL-1L','1L bottle',6,1000,'ambient'),
  ('f0000000-0000-4000-8000-000000000002','e0000000-0000-4000-8000-000000000002','DET-5L','5L jug',4,5200,'ambient')
ON CONFLICT (id) DO NOTHING;

INSERT INTO supplier_offers (id, supplier_id, variant_id, unit_cost_cents, max_units, valid_to) VALUES
  ('00000000-0000-4000-8000-0000000000a1','d0000000-0000-4000-8000-000000000001',
   'f0000000-0000-4000-8000-000000000001',700,600, now() + interval '45 days'),
  ('00000000-0000-4000-8000-0000000000a2','d0000000-0000-4000-8000-000000000001',
   'f0000000-0000-4000-8000-000000000002',1450,300, now() + interval '45 days')
ON CONFLICT (id) DO NOTHING;

-- Campaign 1: SECURED. Past threshold, still open, still improving everyone's price.
INSERT INTO campaigns (id, variant_id, supplier_offer_id, neighborhood_id, pickup_point_id, created_by,
                       state, opens_at, closes_at, secured_at, threshold_units, cap_units, case_pack_units,
                       committed_units, reserved_units, unit_cost_cents, list_price_cents, commission_bps) VALUES
  ('00000000-0000-4000-8000-0000000000c1','f0000000-0000-4000-8000-000000000001',
   '00000000-0000-4000-8000-0000000000a1','b0000000-0000-4000-8000-000000000001',
   'c0000000-0000-4000-8000-000000000001','a0000000-0000-4000-8000-000000000001',
   'open', now() - interval '2 days', now() + interval '2 days', now() - interval '6 hours',
   24,120,6,52,0,700,1200,500)
ON CONFLICT (id) DO NOTHING;

-- Campaign 2: NEAR MISS. 20/24 at close would trigger the one rescue extension.
INSERT INTO campaigns (id, variant_id, supplier_offer_id, neighborhood_id, pickup_point_id, created_by,
                       state, opens_at, closes_at, threshold_units, cap_units, case_pack_units,
                       committed_units, reserved_units, unit_cost_cents, list_price_cents, commission_bps) VALUES
  ('00000000-0000-4000-8000-0000000000c2','f0000000-0000-4000-8000-000000000002',
   '00000000-0000-4000-8000-0000000000a2','b0000000-0000-4000-8000-000000000001',
   'c0000000-0000-4000-8000-000000000002','a0000000-0000-4000-8000-000000000002',
   'open', now() - interval '3 days', now() + interval '4 hours',
   24,80,4,20,0,1450,2200,500)
ON CONFLICT (id) DO NOTHING;

-- Tier ladders. Price must never increase with quantity (validate_tiers enforces
-- this in the application; the DB enforces uniqueness of each break).
INSERT INTO price_tiers (campaign_id, min_units, unit_price_cents) VALUES
  ('00000000-0000-4000-8000-0000000000c1',24,1200),
  ('00000000-0000-4000-8000-0000000000c1',48,1150),
  ('00000000-0000-4000-8000-0000000000c1',96,1100),
  ('00000000-0000-4000-8000-0000000000c2',24,2200),
  ('00000000-0000-4000-8000-0000000000c2',48,2100)
ON CONFLICT (campaign_id, min_units) DO NOTHING;

-- Orders against campaign 1. Note the early buyer quoted 1200 while the campaign
-- now sits in the 48-unit tier: at lock they will be charged 1150, not 1200.
INSERT INTO orders (id, campaign_id, buyer_id, pickup_point_id, state, quantity,
                    quoted_unit_price_cents, idempotency_key) VALUES
  ('00000000-0000-4000-8000-0000000000d1','00000000-0000-4000-8000-0000000000c1',
   'a0000000-0000-4000-8000-000000000003','c0000000-0000-4000-8000-000000000001','committed',24,1200,'seed-order-1'),
  ('00000000-0000-4000-8000-0000000000d2','00000000-0000-4000-8000-0000000000c1',
   'a0000000-0000-4000-8000-000000000004','c0000000-0000-4000-8000-000000000001','committed',28,1200,'seed-order-2'),
  ('00000000-0000-4000-8000-0000000000d3','00000000-0000-4000-8000-0000000000c2',
   'a0000000-0000-4000-8000-000000000005','c0000000-0000-4000-8000-000000000002','committed',20,2200,'seed-order-3')
ON CONFLICT (id) DO NOTHING;

INSERT INTO payment_intents (order_id, psp, psp_intent_ref, state, authorized_amount_cents,
                             authorization_expires_at) VALUES
  ('00000000-0000-4000-8000-0000000000d1','stripe','auth_seed_1','authorized',24*1200, now() + interval '6 days'),
  ('00000000-0000-4000-8000-0000000000d2','stripe','auth_seed_2','authorized',28*1200, now() + interval '6 days'),
  ('00000000-0000-4000-8000-0000000000d3','stripe','auth_seed_3','authorized',20*2200, now() + interval '5 days')
ON CONFLICT (order_id) DO NOTHING;

COMMIT;
