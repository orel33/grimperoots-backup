ARG PHP_IMAGE=php:8.3-apache-bookworm
FROM ${PHP_IMAGE}

ARG LOCAL_UID=1000
ARG LOCAL_GID=1000

RUN apt-get update \
    && apt-get install -y --no-install-recommends libfreetype6-dev libjpeg62-turbo-dev libpng-dev libzip-dev libonig-dev \
    && docker-php-ext-configure gd --with-freetype --with-jpeg \
    && docker-php-ext-install -j2 mysqli pdo_mysql gd mbstring zip \
    && a2enmod rewrite headers \
    && groupadd --gid "${LOCAL_GID}" grimperoots \
    && useradd --uid "${LOCAL_UID}" --gid grimperoots --no-create-home grimperoots \
    && sed -i 's/Listen 80/Listen 8080/' /etc/apache2/ports.conf \
    && sed -i 's/APACHE_RUN_USER=www-data/APACHE_RUN_USER=grimperoots/; s/APACHE_RUN_GROUP=www-data/APACHE_RUN_GROUP=grimperoots/' /etc/apache2/envvars \
    && mkdir -p /var/run/apache2 /var/lock/apache2 \
    && chown -R grimperoots:grimperoots /var/run/apache2 /var/lock/apache2 /var/log/apache2 \
    && rm -rf /var/lib/apt/lists/*

COPY apache.conf /etc/apache2/sites-available/000-default.conf
COPY php-local.ini /usr/local/etc/php/conf.d/grimperoots-local.ini

USER grimperoots
EXPOSE 8080
