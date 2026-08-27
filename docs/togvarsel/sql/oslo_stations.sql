CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.togvarsel_oslo_stations` AS
SELECT * FROM UNNEST([
 STRUCT('NSR:StopPlace:418' AS stop_place,'Asker' AS station,'west' AS corridor, 14 AS km_from_oslo),
 ('NSR:StopPlace:319','Høn','west',13),('NSR:StopPlace:368','Vakås','west',12),('NSR:StopPlace:295','Hvalstad','west',11),
 ('NSR:StopPlace:577','Billingstad','west',10),('NSR:StopPlace:648','Slependen','west',9),('NSR:StopPlace:610','Sandvika','west',8),
 ('NSR:StopPlace:493','Blommenholm','west',7),('NSR:StopPlace:303','Høvik','west',6),('NSR:StopPlace:706','Stabekk','west',5),
 ('NSR:StopPlace:157','Lysaker','west',4),('NSR:StopPlace:152','Skøyen','west',3),
 ('NSR:StopPlace:288','Nationaltheatret','central',1),('NSR:StopPlace:337','Oslo S','central',0),
 ('NSR:StopPlace:529','Bryn','northeast',3),('NSR:StopPlace:385','Alna','northeast',4),('NSR:StopPlace:305','Nyland','northeast',5),
 ('NSR:StopPlace:359','Grorud','northeast',6),('NSR:StopPlace:167','Haugenstua','northeast',7),('NSR:StopPlace:313','Høybråten','northeast',8),
 ('NSR:StopPlace:163','Lørenskog','northeast',9),('NSR:StopPlace:41','Hanaborg','northeast',10),('NSR:StopPlace:208','Fjellhamar','northeast',11),
 ('NSR:StopPlace:716','Strømmen','northeast',12),('NSR:StopPlace:101','Sagdalen','northeast',13),('NSR:StopPlace:451','Lillestrøm','northeast',14),
 ('NSR:StopPlace:279','Nordstrand','south',3),('NSR:StopPlace:433','Ljan','south',4),('NSR:StopPlace:287','Hauketo','south',5),
 ('NSR:StopPlace:202','Holmlia','south',6),('NSR:StopPlace:60','Rosenholm','south',7),('NSR:StopPlace:572','Kolbotn','south',8),
 ('NSR:StopPlace:680','Solbråtan','south',9),('NSR:StopPlace:230','Myrvoll','south',10),('NSR:StopPlace:6','Greverud','south',11),
 ('NSR:StopPlace:323','Oppegård','south',12),('NSR:StopPlace:396','Vevelstad','south',13),('NSR:StopPlace:375','Langhus','south',14),
 ('NSR:StopPlace:127','Ski','south',15)
])
